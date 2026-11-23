"""Project-scoped notification inbox on the proof writer; reads never ACK."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.execution import AuthorityRecord, ExecutionStopRecord, ReviewFault
from jeff.application.execution.watch import (
    WATCH_LIMIT,
    JobNotification,
    WatchInbox,
    WatchScope,
    notification_id_for,
)
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState

if TYPE_CHECKING:
    from .postgres_storage import Connection

WATCH_DDL = (
    "CREATE TABLE jeff_proof.watches (approval_id uuid PRIMARY KEY "
    "REFERENCES jeff_proof.authorities(approval_id), project_id uuid NOT NULL "
    "REFERENCES jeff_proof.projects(project_id), notification_id uuid NOT NULL UNIQUE, "
    "acknowledged boolean NOT NULL DEFAULT false)",
    "CREATE INDEX proof_pending_watches ON jeff_proof.watches(project_id,approval_id) "
    "WHERE NOT acknowledged",
)


def _notice(
    record: AuthorityRecord, stop_facts: object, result_facts: object
) -> JobNotification | None:
    outcome: str
    identity = notification_id_for(record.project_id, record.job_id)
    stop = None if stop_facts is None else ExecutionStopRecord.model_validate(stop_facts).terminal
    if stop is not None and stop.outcome in {"cancelled", "timed_out"}:
        outcome, code = stop.outcome, None
    elif record.claim_state == "unknown":
        outcome, code = "interrupted", None
    elif record.claim_state == "finished" and isinstance(result_facts, dict):
        observed = result_facts.get("outcome")
        if (
            result_facts.get("disposition") != "completed"
            or not isinstance(observed, dict)
            or type(code := observed.get("exit_code")) is not int
        ):
            raise StorageFault("storage_unrecognized")
        outcome = "passed" if code == 0 else "failed"
    elif record.state in {
        AuthorityState.INVALIDATED,
        AuthorityState.DENIED,
        AuthorityState.EXPIRED,
    }:
        outcome, code = "not_started", None
    else:
        return None
    return JobNotification.model_validate(
        dict(
            notification_id=identity,
            job_id=record.job_id,
            outcome=outcome,
            exit_code=code,
        )
    )


_JOIN = (
    " FROM jeff_proof.watches w JOIN jeff_proof.authorities a USING (approval_id) "
    "LEFT JOIN jeff_proof.stops s USING (approval_id) "
    "LEFT JOIN jeff_proof.results r USING (approval_id) "
    "WHERE w.project_id=%s AND a.payload->>'project_id'=%s "
    "AND a.payload->>'canonical_root'=%s"
)


class PostgresWatches:
    def __init__(
        self, transaction: Callable[[float, bool], AbstractContextManager[Connection]], owner: UUID
    ) -> None:
        self._transaction = transaction
        self._owner = owner

    def watch(self, scope: WatchScope, *, deadline: float) -> WatchInbox:
        project = scope.project_id
        path = str(scope.canonical_root)
        with self._transaction(deadline, scope.operation != "watch.pending") as connection:
            if (
                scope.owner_instance_id != self._owner
                or connection.execute(
                    "SELECT 1 FROM jeff_proof.projects WHERE project_id=%s AND canonical_root=%s",
                    (project, path),
                ).fetchone()
                is None
            ):
                raise ReviewFault("execution_not_found")
            if scope.operation == "watch.job":
                return self._register(connection, scope)
            params = (project, str(project), path)
            if scope.operation == "watch.pending":
                rows = connection.execute(
                    "SELECT a.payload,w.notification_id,s.facts,r.facts"
                    + _JOIN
                    + " AND NOT w.acknowledged ORDER BY a.approval_id LIMIT %s",
                    (*params, WATCH_LIMIT + 1),
                ).fetchall()
                if len(rows) > WATCH_LIMIT:
                    raise StorageFault("storage_unrecognized")
                notices = []
                for raw, identity, stop, result in rows:
                    record = AuthorityRecord.model_validate(raw)
                    if identity != notification_id_for(project, record.job_id):
                        raise StorageFault("storage_unrecognized")
                    if (notice := _notice(record, stop, result)) is not None:
                        notices.append(notice)
                return WatchInbox(project_id=project, notifications=tuple(notices))
            row = connection.execute(
                "SELECT a.payload,w.acknowledged,s.facts,r.facts"
                + _JOIN
                + " AND w.notification_id=%s",
                (*params, scope.target),
            ).fetchone()
            if row is None:
                raise ReviewFault("execution_not_found")
            raw, acknowledged, stop, result = row
            record = AuthorityRecord.model_validate(raw)
            if scope.target != notification_id_for(project, record.job_id):
                raise StorageFault("storage_unrecognized")
            if not acknowledged:
                if _notice(record, stop, result) is None:
                    raise ReviewFault("execution_watch_not_ready")
                connection.execute(
                    "UPDATE jeff_proof.watches SET acknowledged=true WHERE approval_id=%s",
                    (record.approval_id,),
                )
            return WatchInbox(project_id=project, acknowledged_id=scope.target)

    @staticmethod
    def _register(connection: Connection, scope: WatchScope) -> WatchInbox:
        assert scope.target is not None
        project, job = scope.project_id, scope.target
        row = connection.execute(
            "SELECT a.approval_id,w.notification_id FROM jeff_proof.authorities a "
            "LEFT JOIN jeff_proof.watches w USING (approval_id) WHERE a.payload->>'job_id'=%s "
            "AND a.payload->>'project_id'=%s AND a.payload->>'canonical_root'=%s",
            (str(job), str(project), str(scope.canonical_root)),
        ).fetchone()
        if row is None:
            raise ReviewFault("execution_not_found")
        approval, prior = row
        identity = notification_id_for(project, job)
        if prior is None:
            count = connection.execute(
                "SELECT count(*) FROM jeff_proof.watches WHERE project_id=%s AND NOT acknowledged",
                (project,),
            ).fetchone()
            if count is None or not isinstance(count[0], int) or count[0] >= WATCH_LIMIT:
                raise ReviewFault("execution_watch_capacity")
            connection.execute(
                "INSERT INTO jeff_proof.watches (approval_id,project_id,notification_id) "
                "VALUES (%s,%s,%s)",
                (approval, project, identity),
            )
        elif prior != identity:
            raise StorageFault("storage_unrecognized")
        return WatchInbox(project_id=project, watched_job_id=job, notification_id=identity)
