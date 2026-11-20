"""Clock-bounded reviews, scoped decisions and durable request deduplication."""

import math
import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from jeff.application.execution import (
    ActionManifest,
    ExecutionData,
    ReviewFault,
    ReviewOperation,
    ReviewRecord,
)
from jeff.application.history import INT64_MAX
from jeff.application.storage import StorageFault
from jeff.domain.execution import (
    LIVE_REVIEW_LIMIT,
    REVIEW_RECORD_LIMIT,
    REVIEW_TTL_SECONDS,
    ReviewState,
    canonical_bytes,
    fingerprint,
)

from .binding import _method
from .catalog import _REVIEW_COLUMNS

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _review_time(self: "SQLiteStorage") -> tuple[int, float, bool]:
    wall, mono = self._review_clock(), self._review_monotonic()
    if (
        type(wall) is not int
        or not 0 <= wall <= INT64_MAX - 600000
        or not math.isfinite(mono)
        or mono < 0
        or (self._review_wall_high is not None and wall < self._review_wall_high)
        or (self._review_mono_high is not None and mono < self._review_mono_high)
    ):
        self._review_clock_bad = True
    if self._review_clock_bad:
        return self._review_wall_high or 0, self._review_mono_high or 0.0, False
    self._review_wall_high, self._review_mono_high = wall, mono
    return wall, mono, True


def _review_record(row: tuple[object, ...]) -> ReviewRecord:
    data = dict(zip(_REVIEW_COLUMNS.split(","), row, strict=True))
    manifest = ActionManifest.model_validate_json(str(data["manifest_json"]))
    if (
        str(manifest.profile.project_id) != data["project_id"]
        or str(manifest.canonical_root) != data["canonical_root"]
        or manifest.profile.fingerprint != data["configuration_digest"]
    ):
        raise StorageFault("storage_schema_unsupported")
    state = ReviewState(str(data["state"]))
    reasons = {
        ReviewState.PENDING: "approval_required",
        ReviewState.APPROVED: "execution_unavailable",
        ReviewState.DENIED: "approval_denied",
        ReviewState.EXPIRED: "approval_expired",
        ReviewState.INVALIDATED: "approval_invalidated",
    }
    return ReviewRecord.model_validate(
        {
            "sequence": data["sequence"],
            "proposal_id": data["proposal_id"],
            "approval_id": data["approval_id"],
            "job_id": data["job_id"],
            "manifest": manifest,
            "manifest_digest": data["manifest_digest"],
            "state": state,
            "created_at_ms": data["created_at_ms"],
            "expires_at_ms": data["expires_at_ms"],
            "owner_instance_id": data["owner_instance_id"],
            "job_state": "waiting_for_approval" if state == ReviewState.PENDING else "blocked",
            "reason": reasons[state],
        }
    )


@_method
def _review_work(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    operation: ReviewOperation,
    root: int,
    fd: int,
) -> ExecutionData:
    call = operation.call
    if self.instance_id is None or operation.owner_instance_id != self.instance_id:
        raise ReviewFault("execution_target_mismatch")
    project, path = str(operation.project_id), str(operation.canonical_root)
    if (
        connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?", (project, path)
        ).fetchone()
        is None
    ):
        raise ReviewFault("execution_target_mismatch")
    self._begin_write(connection, root, fd)
    now, mono, healthy = self._review_time()
    active = connection.execute(
        "SELECT proposal_id,expires_at_ms,owner_instance_id FROM execution_reviews "
        "WHERE state IN ('pending','approved') LIMIT ?",
        (LIVE_REVIEW_LIMIT + 1,),
    ).fetchall()
    if len(active) > LIVE_REVIEW_LIMIT:
        raise StorageFault("storage_schema_unsupported")
    for identifier, expires, owner in active:
        reason = None
        if (
            not healthy
            or owner != str(self.instance_id)
            or identifier not in self._review_deadlines
        ):
            reason = "invalidated"
        elif now >= expires or mono >= self._review_deadlines[identifier]:
            reason = "expired"
        if reason is not None:
            connection.execute(
                "UPDATE execution_reviews SET state=? WHERE proposal_id=?", (reason, identifier)
            )
    if healthy:
        cutoff = max(0, now - (self._retention_days or 30) * 86400000)
        connection.execute(
            "DELETE FROM execution_reviews WHERE sequence IN "
            "(SELECT sequence FROM execution_reviews "
            "WHERE state IN ('denied','expired','invalidated') AND created_at_ms<? LIMIT 100)",
            (cutoff,),
        )
    mutating = call.operation in {"propose", "approve", "deny"}
    if (
        mutating
        and connection.execute(
            "SELECT 1 FROM execution_authority_operations WHERE request_id=?",
            (str(call.request_id),),
        ).fetchone()
    ):
        raise ReviewFault("execution_conflict")
    payload_digest = fingerprint(
        {
            "call": call.model_dump(mode="json"),
            "project": project,
            "root": path,
            "manifest": operation.manifest.fingerprint if operation.manifest else None,
            "configuration": operation.configuration_fingerprint,
        }
    )
    prior = (
        connection.execute(
            "SELECT project_id,canonical_root,payload_digest,proposal_id "
            "FROM execution_operations WHERE request_id=?",
            (str(call.request_id),),
        ).fetchone()
        if mutating
        else None
    )
    if prior:
        if prior[:3] != (project, path, payload_digest):
            raise ReviewFault("execution_conflict")
        row = connection.execute(
            "SELECT " + _REVIEW_COLUMNS + " FROM execution_reviews WHERE proposal_id=?",
            (prior[3],),
        ).fetchone()
        if row is None:
            raise ReviewFault("execution_expired")
        self._commit_write(connection, fd)
        return ExecutionData(items=(self._review_record(row),))
    if not healthy and call.operation in {"propose", "approve"}:
        self._commit_write(connection, fd)
        raise ReviewFault("execution_clock")
    if call.operation == "propose":
        manifest = operation.manifest
        if (
            manifest is None
            or manifest.profile.project_id != operation.project_id
            or manifest.canonical_root != operation.canonical_root
        ):
            raise ReviewFault("execution_target_mismatch")
        if manifest.profile.fingerprint != operation.configuration_fingerprint:
            raise ReviewFault("execution_target_mismatch")
        total = connection.execute(
            "SELECT count(*) FROM execution_operations WHERE operation='propose'"
        ).fetchone()[0]
        live = connection.execute(
            "SELECT count(*) FROM execution_reviews WHERE state IN ('pending','approved')"
        ).fetchone()[0]
        if total >= REVIEW_RECORD_LIMIT or live >= LIVE_REVIEW_LIMIT:
            raise ReviewFault("execution_capacity")
        identifier, approval, job = str(uuid4()), str(uuid4()), str(uuid4())
        connection.execute(
            "INSERT INTO execution_reviews (proposal_id,approval_id,job_id,project_id,"
            "canonical_root,owner_instance_id,manifest_json,manifest_digest,"
            "configuration_digest,state,created_at_ms,expires_at_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (
                identifier,
                approval,
                job,
                project,
                path,
                str(self.instance_id),
                canonical_bytes(manifest.model_dump(mode="json")).decode("ascii"),
                manifest.fingerprint,
                manifest.profile.fingerprint,
                now,
                now + REVIEW_TTL_SECONDS * 1000,
            ),
        )
        row = connection.execute(
            "SELECT " + _REVIEW_COLUMNS + " FROM execution_reviews WHERE proposal_id=?",
            (identifier,),
        ).fetchone()
    elif call.operation in {"approve", "deny", "approval.show", "job.show"}:
        column = "job_id" if call.operation == "job.show" else "approval_id"
        row = connection.execute(
            "SELECT "
            + _REVIEW_COLUMNS
            + " FROM execution_reviews WHERE "
            + column
            + "=? AND project_id=? AND canonical_root=?",
            (str(call.object_id), project, path),
        ).fetchone()
        if row is None:
            raise ReviewFault("execution_not_found")
        current = self._review_record(row)
        identifier = str(current.proposal_id)
        if call.operation in {"approve", "deny"}:
            if current.state != ReviewState.PENDING:
                self._commit_write(connection, fd)
                raise ReviewFault(
                    "execution_expired"
                    if current.state in {ReviewState.EXPIRED, ReviewState.INVALIDATED}
                    else "execution_conflict"
                )
            if (
                call.operation == "approve"
                and operation.configuration_fingerprint != current.manifest.profile.fingerprint
            ):
                connection.execute(
                    "UPDATE execution_reviews SET state='invalidated' WHERE proposal_id=?",
                    (identifier,),
                )
                self._commit_write(connection, fd)
                raise ReviewFault("execution_target_mismatch")
            state = "approved" if call.operation == "approve" else "denied"
            connection.execute(
                "UPDATE execution_reviews SET state=? WHERE proposal_id=? AND state='pending'",
                (state, identifier),
            )
            row = connection.execute(
                "SELECT " + _REVIEW_COLUMNS + " FROM execution_reviews WHERE proposal_id=?",
                (identifier,),
            ).fetchone()
    else:
        rows = connection.execute(
            "SELECT "
            + _REVIEW_COLUMNS
            + " FROM execution_reviews WHERE project_id=? AND canonical_root=? "
            "AND sequence>? ORDER BY sequence LIMIT ?",
            (project, path, call.after, call.limit + 1),
        ).fetchall()
        self._commit_write(connection, fd)
        return ExecutionData(
            items=tuple(self._review_record(item) for item in rows[: call.limit]),
            next_after=rows[call.limit - 1][0] if len(rows) > call.limit else None,
        )
    assert row is not None
    result = self._review_record(row)
    if mutating:
        connection.execute(
            "INSERT INTO execution_operations VALUES (?,?,?,?,?,?,?,?)",
            (
                str(call.request_id),
                project,
                path,
                call.operation,
                payload_digest,
                str(result.proposal_id),
                now,
                result.state.value,
            ),
        )
    self._commit_write(connection, fd)
    if call.operation == "propose":
        self._review_deadlines[str(result.proposal_id)] = mono + REVIEW_TTL_SECONDS
    return ExecutionData(items=(result,))
