"""Service journal statements executed by the existing PostgreSQL writer."""

import time
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.compose_lifecycle import (
    ServiceAttempt,
    ServiceAuthorityOperation,
    ServiceAuthorityRecord,
    ServiceAuthorityResult,
    ServiceEvent,
    ExistingServiceAction,
    ServiceRestart,
    ServiceStart,
    ServiceStop,
    append_service_event,
    stop_matches_start,
)
from jeff.application.execution import ReviewFault
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project

from .postgres_execution import TTL_MS, decide, find_service_authority, save_authority

if TYPE_CHECKING:
    from .postgres_storage import Connection, PostgresStorage

SERVICE_DDL = (
    "CREATE TABLE jeff_proof.service_attempts (job_id uuid PRIMARY KEY, "
    "claim_id uuid UNIQUE NOT NULL REFERENCES jeff_proof.authorities(claim_id), "
    "start_job_id uuid UNIQUE REFERENCES jeff_proof.service_attempts(job_id), "
    "attempt jsonb NOT NULL, phase text NOT NULL "
    "CHECK (phase IN ('intent','created','running','stopped','unknown')))",
    "CREATE TABLE jeff_proof.service_events (job_id uuid NOT NULL "
    "REFERENCES jeff_proof.service_attempts(job_id), ordinal smallint NOT NULL "
    "CHECK (ordinal BETWEEN 1 AND 3), event jsonb NOT NULL, PRIMARY KEY (job_id,ordinal))",
)


def provision(storage: "PostgresStorage", *, deadline: float) -> None:
    with storage._transaction(deadline, write=True) as connection:
        for statement in SERVICE_DDL:
            connection.execute(statement)


def authority(
    storage: "PostgresStorage", operation: ServiceAuthorityOperation, *, deadline: float
) -> ServiceAuthorityResult:
    mono = time.monotonic()
    now = time.time_ns() // 1_000_000
    with storage._transaction(deadline, write=operation.operation != "show") as connection:
        if operation.operation == "propose" and connection.execute(
            "SELECT to_regclass('jeff_proof.service_attempts') IS NOT NULL "
            "AND to_regclass('jeff_proof.service_events') IS NOT NULL"
        ).fetchone() != (True,):
            raise StorageFault("storage_unrecognized")
        result, fresh = decide(
            connection, operation, storage.instance_id, now,
            live_proposals={key for key, end in storage._proposal_deadlines.items() if mono < end},
        )
    if fresh and operation.operation == "propose":
        storage._proposal_deadlines[result.approval_id] = mono + TTL_MS / 1000
    if fresh and operation.operation == "claim":
        assert result.claim_id is not None
        storage._launch_claims.add(result.claim_id)
        return ServiceAuthorityResult(record=result, launch_claim=result.claim_id)
    return ServiceAuthorityResult(record=result)


def record(
    storage: "PostgresStorage", attempt: ServiceAttempt, event: ServiceEvent, *, deadline: float
) -> None:
    with storage._transaction(deadline, write=True) as connection:
        live = find_service_authority(connection, attempt.claim_id, by_claim=True)
        now = time.time_ns() // 1_000_000
        append_service_event_transaction(
            connection, attempt, event, storage.instance_id, storage._launch_claims,
            allow_intent=(
                time.monotonic() < storage._proposal_deadlines.get(live.approval_id, 0)
                and live.created_at_ms <= now < live.expires_at_ms
            ),
        )
    if event.phase in ("running", "stopped", "unknown"):
        storage._launch_claims.discard(attempt.claim_id)


def _attempt(record: ServiceAuthorityRecord) -> ServiceAttempt:
    if record.claim_id is None:
        raise ReviewFault("execution_conflict")
    return ServiceAttempt(
        job_id=record.job_id,
        claim_id=record.claim_id,
        worker_instance_id=record.owner_instance_id,
        manifest=record.manifest,
        reviewed_digest=record.manifest_digest,
    )


def _history(connection: "Connection", job_id: UUID) -> tuple[ServiceEvent, ...]:
    rows = connection.execute(
        "SELECT ordinal,event FROM jeff_proof.service_events WHERE job_id=%s ORDER BY ordinal LIMIT 4",
        (job_id,),
    ).fetchall()
    if len(rows) > 3 or [row[0] for row in rows] != list(range(1, len(rows) + 1)):
        raise ReviewFault("execution_conflict")
    return tuple(ServiceEvent.model_validate(row[1]) for row in rows)


def stop_manifest(
    storage: "PostgresStorage", project: Project, job_id: UUID, *, deadline: float,
    restart: bool = False,
) -> ExistingServiceAction:
    with storage._transaction(deadline, write=False) as connection:
        if connection.execute(
            "SELECT to_regclass('jeff_proof.service_attempts') IS NOT NULL "
            "AND to_regclass('jeff_proof.service_events') IS NOT NULL"
        ).fetchone() != (True,):
            raise StorageFault("storage_unrecognized")
        row = connection.execute(
            "SELECT attempt,phase FROM jeff_proof.service_attempts WHERE job_id=%s", (job_id,)
        ).fetchone()
        if row is None:
            raise ReviewFault("execution_not_found")
        source = ServiceAttempt.model_validate(row[0])
        target = source.manifest.target
        if (target.project_id, target.root) != (project.project_id, project.root):
            raise ReviewFault("execution_not_found")
        if not isinstance(source.manifest, ServiceStart) or row[1] != "running":
            raise ReviewFault("execution_conflict")
        history = _history(connection, job_id)
        if not history or history[-1].resources is None:
            raise ReviewFault("execution_conflict")
        identity = dict(
            target=target, start_job_id=job_id, start_manifest_digest=source.reviewed_digest,
            resources=history[-1].resources,
        )
        proposed = (
            ServiceRestart.model_validate(identity | {"original": source.manifest})
            if restart else ServiceStop.model_validate(identity)
        )
        validate_new_stop(connection, proposed)
        return proposed


def validate_new_stop(connection: "Connection", stop: ExistingServiceAction) -> None:
    row = connection.execute(
        "SELECT attempt,phase FROM jeff_proof.service_attempts WHERE job_id=%s",
        (stop.start_job_id,),
    ).fetchone()
    if row is None or row[1] != "running":
        raise ReviewFault("execution_conflict")
    start = ServiceAttempt.model_validate(row[0])
    record = find_service_authority(connection, start.claim_id, by_claim=True)
    if (
        record.claim_state != "finished"
        or start != _attempt(record)
        or not stop_matches_start(stop, start, _history(connection, start.job_id))
        or connection.execute(
            "SELECT 1 FROM jeff_proof.service_attempts WHERE start_job_id=%s",
            (stop.start_job_id,),
        ).fetchone() is not None
    ):
        raise ReviewFault("execution_conflict")


def append_service_event_transaction(
    connection: "Connection", attempt: ServiceAttempt, event: ServiceEvent, owner: UUID,
    confirmed: set[UUID], *, allow_intent: bool,
) -> None:
    """Validate and append facts under the caller's single writer transaction."""
    if attempt.worker_instance_id != owner or attempt.claim_id not in confirmed:
        raise ReviewFault("execution_conflict")
    live = find_service_authority(connection, attempt.claim_id, by_claim=True)
    if live.claim_state != "claimed" or _attempt(live) != attempt:
        raise ReviewFault("execution_conflict")
    row = connection.execute(
        "SELECT attempt,phase FROM jeff_proof.service_attempts WHERE job_id=%s",
        (attempt.job_id,),
    ).fetchone()
    history = _history(connection, attempt.job_id) if row is not None else ()
    if row is not None and (
        ServiceAttempt.model_validate(row[0]) != attempt or not history or row[1] != history[-1].phase
    ):
        raise ReviewFault("execution_conflict")
    try:
        updated = append_service_event(attempt, history, event)
    except ValueError:
        raise ReviewFault("execution_conflict") from None
    if event.phase == "intent":
        if not allow_intent:
            raise ReviewFault("execution_expired")
        action = attempt.manifest
        if not isinstance(action, ServiceStart):
            validate_new_stop(connection, action)
        # The unique reference reserves the terminal stop only. Restart provenance
        # remains in the immutable reviewed manifest and is verified above.
        stop_source = action.start_job_id if isinstance(action, ServiceStop) else None
        connection.execute(
            "INSERT INTO jeff_proof.service_attempts VALUES (%s,%s,%s,%s::jsonb,%s)",
            (attempt.job_id, attempt.claim_id, stop_source,
             attempt.model_dump_json(), event.phase),
        )
    else:
        connection.execute(
            "UPDATE jeff_proof.service_attempts SET phase=%s WHERE job_id=%s",
            (event.phase, attempt.job_id),
        )
    connection.execute(
        "INSERT INTO jeff_proof.service_events VALUES (%s,%s,%s::jsonb)",
        (attempt.job_id, len(updated), event.model_dump_json()),
    )
    if event.phase in ("running", "stopped", "unknown"):
        terminal = ServiceAuthorityRecord.model_validate(
            live.model_dump()
            | {"claim_state": "unknown" if event.phase == "unknown" else "finished"}
        )
        save_authority(connection, terminal)
