"""Declared work routing and registry SQL on the same connection-owning actor."""

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID

from jeff.application.execution import (
    AuthorityOperation,
    DockerCompletion,
    DockerLaunchEvent,
    DockerLaunchScope,
    ExecutionCancelCall,
    ExecutionInspectionScope,
    ExecutionListScope,
    ExecutionStopCapture,
    ExecutionStopObservation,
    ExecutionStopRequest,
    ProposalRequestScope,
    ReviewFault,
    ReviewOperation,
)
from jeff.application.execution.watch import WatchScope
from jeff.application.history import HistoryRead, StreamRead
from jeff.application.runtime import ControlFault
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project

from .binding import _method
from .catalog import _AUTHORITY_COLUMNS
from .work import _Work

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _execute(
    self: "SQLiteStorage", connection: sqlite3.Connection, work: _Work, root: int, fd: int
) -> object:
    self._check(root, fd)
    if work.operation == "cancel":
        assert isinstance(work.payload, ExecutionCancelCall)
        call = work.payload
        row = connection.execute(
            "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE approval_id=?",
            (str(call.approval_id),),
        ).fetchone()
        if row is None:
            raise ReviewFault("execution_not_found")
        target = self._authority_record(row)
        return self._request_stop(
            connection,
            ExecutionStopRequest(
                project_id=target.project_id,
                canonical_root=target.canonical_root,
                owner_instance_id=cast(UUID, self.instance_id),
                approval_id=call.approval_id,
                request_id=call.request_id,
                manifest_digest=call.reviewed_manifest_digest,
            ),
            root,
            fd,
        )
    if work.operation == "stop_request":
        assert isinstance(work.payload, ExecutionStopRequest)
        return self._request_stop(connection, work.payload, root, fd)
    if work.operation == "stop_finish":
        if isinstance(work.payload, ExecutionStopCapture):
            return self._finish_stop(
                connection, work.payload.observation, root, fd, capture=work.payload.output
            )
        assert isinstance(work.payload, ExecutionStopObservation)
        return self._finish_stop(connection, work.payload, root, fd)
    if work.operation == "stop_inspect":
        assert isinstance(work.payload, ExecutionInspectionScope)
        record = self._stop_target(connection, work.payload)
        return self._read_stop(connection, record, work.payload)
    if work.operation == "execution_complete":
        assert isinstance(work.payload, DockerCompletion)
        return self._complete_execution(connection, work.payload, root, fd)
    if work.operation == "execution_result":
        assert isinstance(work.payload, DockerLaunchScope)
        return self._execution_result(connection, work.payload)
    if work.operation == "launch_record":
        assert isinstance(work.payload, DockerLaunchEvent)
        self._launch_record(connection, work.payload, root, fd)
        return None
    if work.operation == "launch_status":
        assert isinstance(work.payload, DockerLaunchScope)
        return self._launch_status(connection, work.payload)
    if work.operation == "execution_inspect":
        assert isinstance(work.payload, ExecutionInspectionScope)
        return self._inspect_execution(connection, work.payload, root, fd)
    if work.operation == "execution_list":
        assert isinstance(work.payload, ExecutionListScope)
        return self._list_executions(connection, work.payload)
    if work.operation == "execution_watch":
        assert isinstance(work.payload, WatchScope)
        return self._watch(connection, work.payload, root, fd)
    if work.operation == "proposal_lookup":
        assert isinstance(work.payload, ProposalRequestScope)
        return self._lookup_proposal(connection, work.payload)
    if work.operation == "authority":
        assert isinstance(work.payload, AuthorityOperation)
        return self._authority_work(connection, work.payload, root, fd)
    if work.operation == "review":
        assert isinstance(work.payload, ReviewOperation)
        return self._review_work(connection, work.payload, root, fd)
    if work.operation == "retire":
        return self._retire(connection, root, fd)
    if work.operation == "stream":
        assert isinstance(work.payload, StreamRead)
        return self._stream_page(connection, work.payload, root, fd)
    if work.operation == "history":
        assert isinstance(work.payload, HistoryRead)
        return self._history(connection, work.payload, root, fd)
    if work.operation in ("observe", "trace", "replay", "debug"):
        return self._activity_work(connection, work, root, fd)
    if work.operation == "root_claimed":
        assert isinstance(work.payload, Path)
        return (
            connection.execute(
                "SELECT 1 FROM projects WHERE canonical_root=?", (str(work.payload),)
            ).fetchone()
            is not None
        )
    if work.operation not in ("index", "lookup"):
        return self._receipt_work(connection, work, root, fd)
    assert isinstance(work.payload, Project)
    project_id, path = str(work.payload.project_id), str(work.payload.root)
    writing = work.operation == "index"
    if writing:
        self._begin_write(connection, root, fd)
    rows = connection.execute(
        "SELECT project_id,canonical_root FROM projects WHERE project_id=? OR canonical_root=?",
        (project_id, path),
    ).fetchall()
    if rows and rows != [(project_id, path)]:
        raise StorageFault("registry_conflict")
    created = not rows
    if writing and created:
        connection.execute(
            "INSERT INTO projects(project_id,canonical_root) VALUES (?,?)", (project_id, path)
        )
    self._check(root, fd)
    try:
        if writing:
            if created:
                self._commit_write(connection, fd)
            else:
                self._rollback_write(connection, fd)
        self._check(root, fd)
    except (sqlite3.Error, StorageFault, ControlFault, OSError):
        if writing and not connection.in_transaction:
            raise StorageFault("storage_outcome_unconfirmed") from None
        raise
    return created if writing else bool(rows)
