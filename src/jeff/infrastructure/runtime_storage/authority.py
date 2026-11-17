"""Single-use durable execution authority; retries return facts, not launch permission."""

import sqlite3
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    AuthorityResult,
    ExecutionManifest,
    ReviewFault,
)
from jeff.application.storage import StorageFault
from jeff.domain.execution import (
    LIVE_REVIEW_LIMIT,
    REVIEW_RECORD_LIMIT,
    REVIEW_TTL_SECONDS,
    AuthorityState,
    canonical_bytes,
    fingerprint,
)

from .binding import _method
from .catalog import _AUTHORITY_COLUMNS

if TYPE_CHECKING:
    from .store import SQLiteStorage


def _authority_record(row: tuple[object, ...]) -> AuthorityRecord:
    values = dict(zip(_AUTHORITY_COLUMNS.split(","), row, strict=True))
    values["manifest"] = ExecutionManifest.model_validate_json(str(values.pop("manifest_json")))
    return AuthorityRecord.model_validate(values)


def _authority_payload_digest(operation: AuthorityOperation) -> str:
    project, path = str(operation.project_id), str(operation.canonical_root)
    return fingerprint(
        {
            "operation": operation.operation,
            "request": str(operation.request_id),
            "project": project,
            "root": path,
            "owner": str(operation.owner_instance_id),
            "approval": str(operation.approval_id) if operation.approval_id else None,
            "manifest": operation.manifest.fingerprint if operation.manifest else None,
            "current": operation.current_manifest_digest,
        }
    )


@_method
def _authority_work(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    operation: AuthorityOperation,
    root: int,
    fd: int,
) -> AuthorityResult:
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
    live = connection.execute(
        "SELECT proposal_id,expires_at_ms,owner_instance_id FROM execution_authorities "
        "WHERE state IN ('pending','approved') LIMIT ?",
        (LIVE_REVIEW_LIMIT + 1,),
    ).fetchall()
    if len(live) > LIVE_REVIEW_LIMIT:
        raise StorageFault("storage_schema_unsupported")
    for identifier, expires, owner in live:
        state = None
        if (
            not healthy
            or owner != str(self.instance_id)
            or identifier not in self._authority_deadlines
        ):
            state = "invalidated"
        elif now >= expires or mono >= self._authority_deadlines[identifier]:
            state = "expired"
        if state is not None:
            connection.execute(
                "UPDATE execution_authorities SET state=? WHERE proposal_id=?",
                (state, identifier),
            )
    mutating = operation.operation != "show"
    digest = self._authority_payload_digest(operation)
    if (
        mutating
        and connection.execute(
            "SELECT 1 FROM execution_operations WHERE request_id=?",
            (str(operation.request_id),),
        ).fetchone()
    ):
        raise ReviewFault("execution_conflict")
    prior = (
        connection.execute(
            "SELECT payload_digest,proposal_id FROM execution_authority_operations "
            "WHERE request_id=?",
            (str(operation.request_id),),
        ).fetchone()
        if mutating
        else None
    )
    if prior:
        if prior[0] != digest:
            raise ReviewFault("execution_conflict")
        row = connection.execute(
            "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE proposal_id=?",
            (prior[1],),
        ).fetchone()
        if row is None:
            raise StorageFault("storage_schema_unsupported")
        result = AuthorityResult(record=self._authority_record(row))
        self._commit_write(connection, fd)
        return result  # Facts only: never reconstruct the first launch permission.
    if not healthy and operation.operation in {"propose", "approve", "claim"}:
        self._commit_write(connection, fd)
        raise ReviewFault("execution_clock")
    claim_id: UUID | None = None
    if operation.operation == "propose":
        manifest = operation.manifest
        if (
            manifest is None
            or manifest.profile.project_id != operation.project_id
            or manifest.profile.canonical_root != operation.canonical_root
            or manifest.fingerprint != operation.current_manifest_digest
        ):
            raise ReviewFault("execution_target_mismatch")
        count = connection.execute("SELECT count(*) FROM execution_authorities").fetchone()[0]
        active = connection.execute(
            "SELECT count(*) FROM execution_authorities WHERE state IN ('pending','approved')"
        ).fetchone()[0]
        if count >= REVIEW_RECORD_LIMIT or active >= LIVE_REVIEW_LIMIT:
            raise ReviewFault("execution_capacity")
        identifier = str(uuid4())
        connection.execute(
            "INSERT INTO execution_authorities (proposal_id,approval_id,job_id,project_id,"
            "canonical_root,owner_instance_id,manifest_json,manifest_digest,state,created_at_ms,"
            "expires_at_ms) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)",
            (
                identifier,
                str(uuid4()),
                str(uuid4()),
                project,
                path,
                str(self.instance_id),
                canonical_bytes(manifest.model_dump(mode="json")).decode("ascii"),
                manifest.fingerprint,
                now,
                now + REVIEW_TTL_SECONDS * 1000,
            ),
        )
    else:
        row = connection.execute(
            "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities "
            "WHERE approval_id=? AND project_id=? AND canonical_root=?",
            (str(operation.approval_id), project, path),
        ).fetchone()
        if row is None:
            raise ReviewFault("execution_not_found")
        record = self._authority_record(row)
        identifier = str(record.proposal_id)
        if operation.operation == "show":
            self._commit_write(connection, fd)
            return AuthorityResult(record=record)
        required = (
            AuthorityState.APPROVED if operation.operation == "claim" else AuthorityState.PENDING
        )
        if record.state != required:
            self._commit_write(connection, fd)
            raise ReviewFault(
                "execution_expired"
                if record.state in {AuthorityState.EXPIRED, AuthorityState.INVALIDATED}
                else "execution_conflict"
            )
        if operation.operation in {"approve", "claim"} and (
            operation.current_manifest_digest != record.manifest_digest
        ):
            connection.execute(
                "UPDATE execution_authorities SET state='invalidated' WHERE proposal_id=?",
                (identifier,),
            )
            self._commit_write(connection, fd)
            raise ReviewFault("execution_target_mismatch")
        if operation.operation == "claim":
            if connection.execute(
                "SELECT 1 FROM execution_authorities WHERE claim_state='unknown' LIMIT 1"
            ).fetchone():
                raise ReviewFault("execution_outcome_unknown")
            if connection.execute(
                "SELECT 1 FROM execution_authorities WHERE claim_state='claimed' LIMIT 1"
            ).fetchone():
                raise ReviewFault("execution_capacity")
            claim_id = uuid4()
            connection.execute(
                "UPDATE execution_authorities SET state='consumed',claim_id=?,"
                "claim_state='claimed',"
                "claimed_at_ms=? WHERE proposal_id=?",
                (str(claim_id), now, identifier),
            )
        else:
            state = "approved" if operation.operation == "approve" else "denied"
            connection.execute(
                "UPDATE execution_authorities SET state=? WHERE proposal_id=?",
                (state, identifier),
            )
    row = connection.execute(
        "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE proposal_id=?",
        (identifier,),
    ).fetchone()
    assert row is not None
    result = AuthorityResult(record=self._authority_record(row), launch_claim=claim_id)
    connection.execute(
        "INSERT INTO execution_authority_operations VALUES (?,?,?)",
        (str(operation.request_id), digest, identifier),
    )
    self._commit_write(connection, fd)  # Failure/unknown acknowledgement returns no launch claim.
    if operation.operation == "propose":
        self._authority_deadlines[identifier] = mono + REVIEW_TTL_SECONDS
    elif operation.operation == "deny":
        self._authority_deadlines.pop(identifier, None)
    return result
