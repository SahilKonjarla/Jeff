"""Scoped persisted execution/proposal inspection without manufacturing permission."""

import sqlite3
from typing import TYPE_CHECKING

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    DockerLaunchScope,
    ExecutionInspection,
    ExecutionInspectionScope,
    ExecutionStopView,
    ProposalRequestScope,
    ReviewFault,
)
from jeff.application.storage import StorageFault

from .binding import _method
from .catalog import _AUTHORITY_COLUMNS

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _inspect_execution(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    scope: ExecutionInspectionScope,
    root: int,
    fd: int,
) -> ExecutionInspection:
    # One actor turn: completion cannot race an authority read on this SQL owner.
    record = self._authority_work(
        connection,
        AuthorityOperation(
            operation="show",
            project_id=scope.project_id,
            canonical_root=scope.canonical_root,
            owner_instance_id=scope.owner_instance_id,
            approval_id=scope.approval_id,
        ),
        root,
        fd,
    ).record
    if record.owner_instance_id != scope.owner_instance_id:
        raise ReviewFault("execution_target_mismatch")
    stop = self._read_stop(connection, record, scope)
    has_legacy = (
        connection.execute(
            "SELECT 1 FROM execution_results r JOIN execution_launches l "
            "ON l.attempt_id=r.attempt_id "
            "WHERE l.claim_id=?",
            (str(record.claim_id),),
        ).fetchone()
        is not None
    )
    if (
        stop is not None
        and stop.terminal is not None
        and stop.terminal.outcome != "completed"
        and not has_legacy
    ):
        view, output = self._stop_payload(connection, record, stop)
        return ExecutionInspection(
            record=record, completion_state="stop_recorded", stop=view, stop_output=output
        )
    stop_view = ExecutionStopView.from_record(stop) if stop is not None else None
    if record.claim_id is None:
        return ExecutionInspection(record=record, completion_state="not_claimed", stop=stop_view)
    launch_row = connection.execute(
        "SELECT 1 FROM execution_launches WHERE claim_id=?", (str(record.claim_id),)
    ).fetchone()
    if launch_row is None:
        if record.claim_state == "finished":
            raise StorageFault("storage_schema_unsupported")
        return ExecutionInspection(record=record, completion_state="claim_only", stop=stop_view)
    launch_scope = DockerLaunchScope(
        project_id=scope.project_id,
        canonical_root=scope.canonical_root,
        claim_id=record.claim_id,
    )
    launch = self._launch_status(connection, launch_scope)
    result_row = connection.execute(
        "SELECT 1 FROM execution_results WHERE attempt_id=?", (str(launch.intent.attempt_id),)
    ).fetchone()
    if result_row is None:
        if record.claim_state == "finished":
            raise StorageFault("storage_schema_unsupported")
        return ExecutionInspection(
            record=record, completion_state="launch_without_result", stop=stop_view
        )
    result = self._execution_result(connection, launch_scope)
    if stop is not None:
        stop_view = ExecutionStopView.from_record(
            stop,
            payload_state=result.payload_state,
            retirement_reason=result.retirement_reason,
            recorded_at_ms=result.recorded_at_ms,
        )
    return ExecutionInspection(
        record=record,
        completion_state="recorded",
        result=result,
        stop=stop_view,
    )


@_method
def _lookup_proposal(
    self: "SQLiteStorage", connection: sqlite3.Connection, scope: ProposalRequestScope
) -> AuthorityRecord | None:
    if self.instance_id is None or scope.owner_instance_id != self.instance_id:
        raise ReviewFault("execution_target_mismatch")
    if (
        connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?",
            (str(scope.project_id), str(scope.canonical_root)),
        ).fetchone()
        is None
    ):
        raise ReviewFault("execution_target_mismatch")
    if connection.execute(
        "SELECT 1 FROM execution_operations WHERE request_id=?", (str(scope.request_id),)
    ).fetchone():
        raise ReviewFault("execution_conflict")
    prior = connection.execute(
        "SELECT payload_digest,proposal_id FROM execution_authority_operations WHERE request_id=?",
        (str(scope.request_id),),
    ).fetchone()
    if prior is None:
        return None
    row = connection.execute(
        "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE proposal_id=?",
        (prior[1],),
    ).fetchone()
    if row is None:
        raise StorageFault("storage_schema_unsupported")
    record = self._authority_record(row)
    if (record.project_id, record.canonical_root, record.owner_instance_id) != (
        scope.project_id,
        scope.canonical_root,
        scope.owner_instance_id,
    ):
        raise ReviewFault("execution_conflict")
    original = AuthorityOperation(
        operation="propose",
        request_id=scope.request_id,
        project_id=scope.project_id,
        canonical_root=scope.canonical_root,
        owner_instance_id=scope.owner_instance_id,
        manifest=record.manifest,
        current_manifest_digest=record.manifest_digest,
    )
    if self._authority_payload_digest(original) != prior[0]:
        raise ReviewFault("execution_conflict")
    return record
