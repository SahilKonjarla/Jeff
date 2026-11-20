"""Scope-bound lifecycle target and serialized stop-record validation."""

import sqlite3
from typing import TYPE_CHECKING

from jeff.application.execution import (
    AuthorityRecord,
    DockerExecutionResult,
    ExecutionInspectionScope,
    ExecutionStopRecord,
    ExecutionStopTerminal,
    ReviewFault,
)
from jeff.application.storage import StorageFault
from jeff.domain.execution import fingerprint

from .binding import _method
from .catalog import _AUTHORITY_COLUMNS

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _stop_target(
    self: "SQLiteStorage", connection: sqlite3.Connection, scope: ExecutionInspectionScope
) -> AuthorityRecord:
    if self.instance_id is None or scope.owner_instance_id != self.instance_id:
        raise ReviewFault("execution_target_mismatch")
    row = connection.execute(
        "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities "
        "WHERE approval_id=? AND project_id=? AND canonical_root=?",
        (str(scope.approval_id), str(scope.project_id), str(scope.canonical_root)),
    ).fetchone()
    if row is None:
        raise ReviewFault("execution_not_found")
    record = self._authority_record(row)
    if (
        record.owner_instance_id != scope.owner_instance_id
        or connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?",
            (str(scope.project_id), str(scope.canonical_root)),
        ).fetchone()
        is None
    ):
        raise ReviewFault("execution_target_mismatch")
    return record


def _read_stop(
    connection: sqlite3.Connection, authority: AuthorityRecord, scope: ExecutionInspectionScope
) -> ExecutionStopRecord | None:
    row = connection.execute(
        "SELECT record_json,record_digest FROM execution_lifecycles WHERE proposal_id=?",
        (str(authority.proposal_id),),
    ).fetchone()
    if row is None:
        return None
    try:
        record = ExecutionStopRecord.model_validate_json(row[0])
        if (
            record.scope != scope
            or record.manifest_digest != authority.manifest_digest
            or record.fingerprint != row[1]
        ):
            raise ValueError("Invalid lifecycle binding.")
        return record
    except ValueError:
        raise StorageFault("storage_schema_unsupported") from None


def _write_stop(
    connection: sqlite3.Connection, authority: AuthorityRecord, record: ExecutionStopRecord
) -> None:
    payload = record.model_dump_json()
    if len(payload.encode()) > 32768:
        raise ReviewFault("execution_capacity")
    connection.execute(
        "INSERT INTO execution_lifecycles VALUES (?,?,?) "
        "ON CONFLICT(proposal_id) DO UPDATE SET record_json=excluded.record_json,"
        "record_digest=excluded.record_digest",
        (str(authority.proposal_id), payload, record.fingerprint),
    )


def _result_stop_terminal(result: DockerExecutionResult) -> ExecutionStopTerminal:
    owned = result.owned
    return ExecutionStopTerminal(
        outcome="completed" if result.disposition == "completed" else "unknown",
        evidence="owned",
        cleanup_verified=result.outcome.cleanup_verified,
        exit_code=result.outcome.exit_code,
        attempt_id=owned.intent.attempt_id,
        ownership_digest=fingerprint(
            {
                "intent": owned.intent.fingerprint,
                "container": owned.container_id,
                "birth": owned.created_at,
            }
        ),
        output_facts=result.output_facts,
        output_digest=result.output_digest,
    )
