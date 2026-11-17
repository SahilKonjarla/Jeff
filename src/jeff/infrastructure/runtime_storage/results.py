"""Checked persisted completion and stop-output views; no fabricated available text."""

import json
import sqlite3
from hashlib import sha256
from typing import TYPE_CHECKING

from jeff.application.execution import (
    AuthorityRecord,
    DockerBackendFault,
    DockerExecutionResult,
    DockerLaunchScope,
    DockerOutputFacts,
    DockerOutputSnapshot,
    DockerOwnedContainer,
    ExecutionStopRecord,
    ExecutionStopView,
    ReviewFault,
)
from jeff.application.storage import StorageFault

from .binding import _method

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _execution_result(
    self: "SQLiteStorage", connection: sqlite3.Connection, scope: DockerLaunchScope
) -> DockerExecutionResult:
    launch = self._launch_status(connection, scope)
    row = connection.execute(
        "SELECT sequence,disposition,recorded_at_ms,metadata_json,completion_digest,"
        "stdout,stderr,retirement_reason FROM execution_results WHERE attempt_id=?",
        (str(launch.intent.attempt_id),),
    ).fetchone()
    if row is None:
        raise DockerBackendFault("docker_mutation_unconfirmed")
    try:
        metadata = json.loads(row[3])
        expected = "finished" if row[1] == "completed" else "unknown"
        if launch.claim_state != expected or (
            row[1] == "completed"
            and (launch.uncertain or "cleaned" not in launch.phases or launch.exit_code is None)
        ):
            raise ValueError("Inconsistent completion state.")
        output = (
            None
            if row[5] is None
            else DockerOutputSnapshot.model_validate(
                {**metadata["output_facts"], "stdout": row[5], "stderr": row[6]}
            )
        )
        result = DockerExecutionResult.model_validate(
            {
                **metadata,
                "owned": DockerOwnedContainer.model_validate(
                    {
                        "intent": launch.intent,
                        "container_id": launch.container_id,
                        "created_at": launch.created_at,
                    }
                ),
                "sequence": row[0],
                "disposition": row[1],
                "recorded_at_ms": row[2],
                "completion_digest": row[4],
                "payload_state": "retired" if output is None else "retained",
                "retirement_reason": row[7],
                "output": output,
            }
        )
        now = self._sample_retention_wall()
        if result.output is not None and (
            now is None
            or result.recorded_at_ms < max(0, now - (self._retention_days or 30) * 86400000)
        ):
            result = result.model_copy(
                update={
                    "output": None,
                    "payload_state": "clock_unconfirmed" if now is None else "expired",
                }
            )
        return result
    except (ValueError, TypeError, KeyError):
        raise StorageFault("storage_schema_unsupported") from None


@_method
def _insert_stop_payload(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    authority: AuthorityRecord,
    record: ExecutionStopRecord,
    capture: DockerOutputSnapshot,
) -> None:
    terminal = record.terminal
    assert terminal is not None
    if capture.retention_limit != authority.manifest.profile.output_bytes:
        raise ReviewFault("execution_target_mismatch")
    now = self._sample_retention_wall()
    cutoff = max(0, (now or 0) - (self._retention_days or 30) * 86400000)
    self._retire_execution_payloads(connection, cutoff, age_limit=1)
    occupied = any(r[2] == "quarantined" for r in self._retained_payloads(connection))
    retain = terminal.outcome != "unknown" or not occupied
    connection.execute(
        "INSERT INTO execution_stop_payloads VALUES (?,?,?,?,?,?,?,?,?)",
        (
            str(authority.proposal_id),
            "quarantined" if terminal.outcome == "unknown" else "known",
            now if now is not None else record.requested_at_ms,
            int(now is not None),
            terminal.output_facts.model_dump_json() if terminal.output_facts is not None else "{}",
            terminal.output_digest,
            capture.stdout if retain else None,
            capture.stderr if retain else None,
            None if retain else "capacity",
        ),
    )
    self._retire_execution_payloads(connection, cutoff, age_limit=0)


@_method
def _stop_payload(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    authority: AuthorityRecord,
    record: ExecutionStopRecord,
) -> tuple[ExecutionStopView, DockerOutputSnapshot | None]:
    terminal = record.terminal
    fields: dict[str, object] = dict(
        request_id=record.request_id,
        reason=record.reason,
        requested_at_ms=record.requested_at_ms,
        clock_confirmed=record.clock_confirmed,
        payload_state="missing",
    )
    if terminal is not None:
        fields.update(
            outcome=terminal.outcome,
            evidence=terminal.evidence,
            cleanup_verified=terminal.cleanup_verified,
            exit_code=terminal.exit_code,
            output_facts=terminal.output_facts,
        )
    row = connection.execute(
        "SELECT disposition,recorded_at_ms,clock_confirmed,output_facts_json,output_digest,"
        "stdout,stderr,retirement_reason FROM execution_stop_payloads WHERE proposal_id=?",
        (str(authority.proposal_id),),
    ).fetchone()
    if row is None:
        return ExecutionStopView.model_validate(fields), None
    try:
        if (
            terminal is None
            or terminal.output_facts is None
            or (
                DockerOutputFacts.model_validate_json(row[3]) != terminal.output_facts
                or row[4] != terminal.output_digest
                or row[0] != ("quarantined" if terminal.outcome == "unknown" else "known")
                or terminal.output_facts.retention_limit != authority.manifest.profile.output_bytes
            )
        ):
            raise ValueError("Stop payload binding differs.")
        snapshot = None
        if row[5] is not None:
            snapshot = DockerOutputSnapshot.model_validate(
                {**terminal.output_facts.model_dump(), "stdout": row[5], "stderr": row[6]}
            )
            if sha256(snapshot.model_dump_json().encode("utf-8")).hexdigest() != row[4]:
                raise ValueError("Stop payload digest differs.")
        if snapshot is None:
            state, retirement = "retired", row[7]
        else:
            now = self._sample_retention_wall()
            state, retirement = "retained", None
            if now is None or not row[2]:
                state = "clock_unconfirmed"
            elif row[1] < max(0, now - (self._retention_days or 30) * 86400000):
                state = "expired"
            if state != "retained":
                snapshot = None
        fields.update(payload_state=state, retirement_reason=retirement, recorded_at_ms=row[1])
        return ExecutionStopView.model_validate(fields), snapshot
    except (ValueError, TypeError):
        raise StorageFault("storage_schema_unsupported") from None
