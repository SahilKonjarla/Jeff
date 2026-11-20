"""Scoped persisted execution/proposal inspection without manufacturing permission."""

import sqlite3
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    DockerLaunchScope,
    ExecutionInspection,
    ExecutionInspectionScope,
    ExecutionListScope,
    ExecutionStopView,
    FreshExecutionPage,
    FreshExecutionView,
    ProposalRequestScope,
    ReviewFault,
    parse_execution_cursor,
)
from jeff.application.execution.pytest_report import PYTEST_RUNNER_ARGV
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState

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
    approval_id = scope.approval_id
    if approval_id is None:
        if self.instance_id is None or scope.owner_instance_id != self.instance_id:
            raise ReviewFault("execution_target_mismatch")
        project_scope = (str(scope.project_id), str(scope.canonical_root))
        if scope.latest_pytest:
            row = connection.execute(
                "SELECT approval_id FROM execution_authorities WHERE project_id=? "
                "AND canonical_root=? AND json_extract(manifest_json,'$.authority_kind')"
                "='guarded_execution_v1' "
                "AND json_extract(manifest_json,'$.original.argv[3]')=? "
                "ORDER BY created_at_ms DESC, proposal_id DESC LIMIT 1",
                (*project_scope, PYTEST_RUNNER_ARGV[-1]),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT approval_id FROM execution_authorities WHERE job_id=? "
                "AND project_id=? AND canonical_root=?",
                (str(scope.job_id), *project_scope),
            ).fetchone()
        if row is None:
            raise ReviewFault("execution_not_found")
        approval_id = UUID(row[0])
    record = self._authority_work(
        connection,
        AuthorityOperation(
            operation="show",
            project_id=scope.project_id,
            canonical_root=scope.canonical_root,
            owner_instance_id=scope.owner_instance_id,
            approval_id=approval_id,
        ),
        root,
        fd,
    ).record
    # The current reader/project was checked above. Persisted facts still bind to
    # the original owner; historical observation never transfers mutation authority.
    original_scope = ExecutionInspectionScope(
        project_id=record.project_id,
        canonical_root=record.canonical_root,
        owner_instance_id=record.owner_instance_id,
        approval_id=record.approval_id,
    )
    stop = self._read_stop(connection, record, original_scope)
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
    if launch.intent.record.model_dump(exclude={"claim_state"}) != record.model_dump(
        exclude={"claim_state"}
    ):
        raise StorageFault("storage_schema_unsupported")
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
def _list_executions(
    self: "SQLiteStorage", connection: sqlite3.Connection, scope: ExecutionListScope
) -> FreshExecutionPage:
    if self.instance_id is None or scope.owner_instance_id != self.instance_id:
        raise ReviewFault("execution_target_mismatch")
    project, path = str(scope.project_id), str(scope.canonical_root)
    if (
        connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?", (project, path)
        ).fetchone()
        is None
    ):
        raise ReviewFault("execution_target_mismatch")
    condition = ""
    values: list[object] = [project, path]
    if scope.after is not None:
        stamp, identifier = parse_execution_cursor(scope.after)
        if (
            connection.execute(
                "SELECT 1 FROM execution_authorities WHERE project_id=? AND canonical_root=? "
                "AND created_at_ms=? AND proposal_id=?",
                (project, path, stamp, identifier),
            ).fetchone()
            is None
        ):
            raise ReviewFault("execution_conflict")
        condition = " AND (created_at_ms<? OR (created_at_ms=? AND proposal_id<?))"
        values.extend((stamp, stamp, identifier))
    rows = connection.execute(
        "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities "
        "WHERE project_id=? AND canonical_root=?"
        + condition
        + " ORDER BY created_at_ms DESC,proposal_id DESC LIMIT ?",
        (*values, scope.limit + 1),
    ).fetchall()
    now, mono, healthy = self._review_time()
    items = []
    for row in rows[: scope.limit]:
        record = self._authority_record(row)
        if record.state in (AuthorityState.PENDING, AuthorityState.APPROVED):
            deadline = self._authority_deadlines.get(str(record.proposal_id))
            # Match authority expiry semantics without changing durable rows or permission.
            if not healthy or record.owner_instance_id != self.instance_id or deadline is None:
                record = record.model_copy(update={"state": AuthorityState.INVALIDATED})
            elif now >= record.expires_at_ms or mono >= deadline:
                record = record.model_copy(update={"state": AuthorityState.EXPIRED})
        items.append(FreshExecutionView.from_record(record))
    after = None
    if len(rows) > scope.limit:
        last = items[-1]
        after = f"{last.created_at_ms}:{last.proposal_id}"
    return FreshExecutionPage(
        project_id=scope.project_id,
        canonical_root=scope.canonical_root,
        items=tuple(items),
        next_after=after,
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
