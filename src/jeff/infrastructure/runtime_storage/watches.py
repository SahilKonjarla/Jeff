"""Durable watch and ACK state on the existing SQLite owner."""

import sqlite3
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.execution import ExecutionInspection, ExecutionInspectionScope, ReviewFault
from jeff.application.execution.watch import (
    WATCH_LIMIT,
    JobNotification,
    WatchInbox,
    WatchScope,
    notification_id_for,
)
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState

from .binding import _method

if TYPE_CHECKING:
    from .store import SQLiteStorage


def _notice(project: UUID, inspection: ExecutionInspection) -> JobNotification | None:
    """Project existing terminal facts; a missing report is not a new job outcome."""
    record = inspection.record
    identity = dict(
        notification_id=notification_id_for(project, record.job_id), job_id=record.job_id
    )
    stop = inspection.stop
    if stop is not None and stop.outcome in {"cancelled", "timed_out"}:
        return JobNotification.model_validate(identity | {"outcome": stop.outcome})
    if record.claim_state == "unknown":
        return JobNotification.model_validate(identity | {"outcome": "interrupted"})
    if inspection.result is not None and inspection.result.disposition == "completed":
        code = inspection.result.outcome.exit_code
        return JobNotification.model_validate(
            identity | {"outcome": "passed" if code == 0 else "failed", "exit_code": code}
        )
    if record.state in {AuthorityState.DENIED, AuthorityState.EXPIRED, AuthorityState.INVALIDATED}:
        return JobNotification.model_validate(identity | {"outcome": "not_started"})
    return None


@_method
def _watch(
    self: "SQLiteStorage", connection: sqlite3.Connection, scope: WatchScope, root: int, fd: int
) -> WatchInbox:
    project = scope.project_id
    bounds = (str(project), str(scope.canonical_root))
    if (
        self.instance_id != scope.owner_instance_id
        or connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?", bounds
        ).fetchone()
        is None
    ):
        raise ReviewFault("execution_not_found")

    def inspect(job: UUID) -> ExecutionInspection:
        return self._inspect_execution(
            connection,
            ExecutionInspectionScope(
                project_id=project,
                canonical_root=scope.canonical_root,
                owner_instance_id=scope.owner_instance_id,
                job_id=job,
            ),
            root,
            fd,
        )

    relation = (
        " FROM execution_job_watches w JOIN execution_authorities a ON a.job_id=w.job_id "
        "WHERE a.project_id=? AND a.canonical_root=?"
    )
    if scope.operation == "watch.pending":
        # No delivery mutation here: only an explicit ACK suppresses the stable ID.
        rows = connection.execute(
            "SELECT w.job_id,w.notification_id"
            + relation
            + " AND w.acknowledged=0 ORDER BY a.created_at_ms,a.proposal_id LIMIT ?",
            (*bounds, WATCH_LIMIT + 1),
        ).fetchall()
        if len(rows) > WATCH_LIMIT:
            raise StorageFault("storage_schema_unsupported")
        notifications: list[JobNotification] = []
        for raw_job, raw_notification in rows:
            job = UUID(raw_job)
            if str(notification_id_for(project, job)) != raw_notification:
                raise StorageFault("storage_schema_unsupported")
            if (notice := _notice(project, inspect(job))) is not None:
                notifications.append(notice)
        return WatchInbox(project_id=project, notifications=tuple(notifications))
    assert scope.target is not None
    if scope.operation == "watch.job":
        job = scope.target
        inspect(job)
        notification = notification_id_for(project, job)
        row = connection.execute(
            "SELECT notification_id FROM execution_job_watches WHERE job_id=?", (str(job),)
        ).fetchone()
        if row is not None and row[0] != str(notification):
            raise StorageFault("storage_schema_unsupported")
        if row is None:
            count = connection.execute(
                "SELECT count(*)" + relation + " AND w.acknowledged=0", bounds
            ).fetchone()[0]
            if count >= WATCH_LIMIT:
                raise ReviewFault("execution_watch_capacity")
            self._begin_write(connection, root, fd)
            connection.execute(
                "INSERT INTO execution_job_watches (job_id,notification_id) VALUES (?,?)",
                (str(job), str(notification)),
            )
            self._commit_write(connection, fd)
        return WatchInbox(project_id=project, watched_job_id=job, notification_id=notification)
    row = connection.execute(
        "SELECT w.job_id,w.acknowledged" + relation + " AND w.notification_id=?",
        (*bounds, str(scope.target)),
    ).fetchone()
    if row is None:
        raise ReviewFault("execution_not_found")
    job = UUID(row[0])
    if notification_id_for(project, job) != scope.target:
        raise StorageFault("storage_schema_unsupported")
    if not row[1]:
        if _notice(project, inspect(job)) is None:
            raise ReviewFault("execution_watch_not_ready")
        self._begin_write(connection, root, fd)
        connection.execute(
            "UPDATE execution_job_watches SET acknowledged=1 WHERE job_id=?", (str(job),)
        )
        self._commit_write(connection, fd)
    return WatchInbox(project_id=project, acknowledged_id=scope.target)
