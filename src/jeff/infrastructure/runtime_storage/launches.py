"""Owned launch journal ordering, exact identity and durable observation deduplication."""

import sqlite3
from typing import TYPE_CHECKING

from jeff.application.execution import (
    DockerBackendFault,
    DockerLaunchEvent,
    DockerLaunchIntent,
    DockerLaunchScope,
    DockerLaunchSnapshot,
)
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState, fingerprint

from .binding import _method
from .catalog import _AUTHORITY_COLUMNS

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _launch_status(
    self: "SQLiteStorage", connection: sqlite3.Connection, scope: DockerLaunchScope
) -> DockerLaunchSnapshot:
    if self.instance_id is None:
        raise DockerBackendFault("docker_authority_required")
    row = connection.execute(
        "SELECT l.intent_json,l.intent_digest,l.container_id,l.created_at,"
        "l.observed_container_id,l.exit_code,a.claim_state FROM execution_launches l "
        "JOIN execution_authorities a ON a.claim_id=l.claim_id "
        "WHERE l.claim_id=? AND a.project_id=? AND a.canonical_root=?",
        (str(scope.claim_id), str(scope.project_id), str(scope.canonical_root)),
    ).fetchone()
    if row is None:
        raise DockerBackendFault("docker_ownership_unconfirmed")
    try:
        intent = DockerLaunchIntent.model_validate_json(row[0])
        if intent.fingerprint != row[1] or intent.record.claim_id != scope.claim_id:
            raise ValueError("Invalid persisted launch binding.")
        events = connection.execute(
            "SELECT phase,sequence FROM execution_launch_events WHERE attempt_id=? "
            "ORDER BY sequence LIMIT 7",
            (str(intent.attempt_id),),
        ).fetchall()
        if not events or any(number != index for index, (_, number) in enumerate(events, 1)):
            raise ValueError("Invalid persisted lifecycle order.")
        phases = tuple(phase for phase, _ in events)
        return DockerLaunchSnapshot.model_validate(
            {
                "intent": intent,
                "phases": phases,
                "container_id": row[2],
                "created_at": row[3],
                "observed_container_id": row[4],
                "exit_code": row[5],
                "claim_state": row[6],
                "uncertain": row[6] == "unknown" or "unknown" in phases,
            }
        )
    except ValueError:
        raise StorageFault("storage_schema_unsupported") from None


@_method
def _launch_record(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    event: DockerLaunchEvent,
    root: int,
    fd: int,
) -> None:
    intent = event.intent
    supplied = intent.record
    if self.instance_id is None or supplied.owner_instance_id != self.instance_id:
        raise DockerBackendFault("docker_authority_required")
    row = connection.execute(
        "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE claim_id=?",
        (str(supplied.claim_id),),
    ).fetchone()
    if row is None:
        raise DockerBackendFault("docker_authority_required")
    current = self._authority_record(row)
    if (
        current.state != AuthorityState.CONSUMED
        or current.claim_state not in {"claimed", "unknown"}
        or current.model_dump(exclude={"claim_state"})
        != supplied.model_dump(exclude={"claim_state"})
        or connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?",
            (str(current.project_id), str(current.canonical_root)),
        ).fetchone()
        is None
    ):
        raise DockerBackendFault("docker_authority_required")
    try:
        binding = intent.fingerprint
        payload = intent.model_dump_json()
        if len(payload.encode("utf-8")) > 131072:
            raise ValueError("Launch metadata exceeds its bound.")
        digest = fingerprint(
            {"intent": binding, **event.model_dump(mode="json", exclude={"intent"})}
        )
    except ValueError:
        raise DockerBackendFault("docker_policy_mismatch") from None
    self._begin_write(connection, root, fd)
    existing = connection.execute(
        "SELECT attempt_id,intent_digest FROM execution_launches WHERE claim_id=?",
        (str(current.claim_id),),
    ).fetchone()
    if existing is not None and existing != (str(intent.attempt_id), binding):
        raise DockerBackendFault("docker_authority_required")
    if event.phase in {"create_intent", "start_intent"}:
        # A durable stop is a launch fence, not merely absence of a launch row.
        # The same SQL owner orders this check against claim/stop transactions.
        if connection.execute(
            "SELECT 1 FROM execution_lifecycles WHERE proposal_id=?",
            (str(current.proposal_id),),
        ).fetchone():
            raise DockerBackendFault("docker_authority_required")
        now, mono, healthy = self._review_time()
        original = self._authority_deadlines.get(str(current.proposal_id))
        if (
            current.claim_state != "claimed"
            or not healthy
            or original is None
            or now >= current.expires_at_ms
            or mono >= original
        ):
            raise DockerBackendFault("docker_authority_required")
    if existing is None:
        if event.phase not in {"create_intent", "unknown"}:
            raise DockerBackendFault("docker_authority_required")
        if connection.execute(
            "SELECT 1 FROM execution_launches WHERE attempt_id=?", (str(intent.attempt_id),)
        ).fetchone():
            raise DockerBackendFault("docker_authority_required")
        connection.execute(
            "INSERT INTO execution_launches (attempt_id,claim_id,intent_json,"
            "intent_digest) VALUES (?,?,?,?)",
            (str(intent.attempt_id), str(current.claim_id), payload, binding),
        )
    elif event.phase == "create_intent":
        raise DockerBackendFault("docker_mutation_unconfirmed")
    prior = connection.execute(
        "SELECT event_digest FROM execution_launch_events WHERE attempt_id=? AND phase=?",
        (str(intent.attempt_id), event.phase),
    ).fetchone()
    if prior is not None:
        if event.phase == "start_intent" or prior[0] != digest:
            raise DockerBackendFault("docker_mutation_unconfirmed")
        self._commit_write(connection, fd)
        return  # Observation deduplication never reopens a mutation gate.
    phases = {
        item[0]
        for item in connection.execute(
            "SELECT phase FROM execution_launch_events WHERE attempt_id=?",
            (str(intent.attempt_id),),
        )
    }
    owned = connection.execute(
        "SELECT container_id,created_at,observed_container_id,exit_code "
        "FROM execution_launches WHERE attempt_id=?",
        (str(intent.attempt_id),),
    ).fetchone()
    assert owned is not None
    if (
        (
            event.phase in {"created", "start_intent"}
            and ("unknown" in phases or "cleaned" in phases or current.claim_state != "claimed")
        )
        or (
            event.phase == "created" and ("create_intent" not in phases or "start_intent" in phases)
        )
        or (event.phase == "start_intent" and "created" not in phases)
        or (event.phase == "exited" and ("start_intent" not in phases or "cleaned" in phases))
        or (event.phase == "cleaned" and owned[0] is None)
    ):
        raise DockerBackendFault("docker_authority_required")
    values = (
        event.container_id,
        event.created_at,
        event.observed_container_id,
        event.exit_code,
    )
    if any(
        old is not None and new is not None and old != new
        for old, new in zip(owned, values, strict=True)
    ):
        raise DockerBackendFault("docker_ownership_unconfirmed")
    container = event.container_id or owned[0]
    observed = event.observed_container_id or owned[2]
    if container is not None and observed is not None and container != observed:
        raise DockerBackendFault("docker_ownership_unconfirmed")
    connection.execute(
        "UPDATE execution_launches SET container_id=coalesce(container_id,?),"
        "created_at=coalesce(created_at,?),observed_container_id=coalesce(observed_container_id,?),"
        "exit_code=coalesce(exit_code,?) WHERE attempt_id=?",
        (*values, str(intent.attempt_id)),
    )
    connection.execute(
        "INSERT INTO execution_launch_events VALUES (?,?,?,?)",
        (str(intent.attempt_id), event.phase, len(phases) + 1, digest),
    )
    if event.phase == "unknown":
        connection.execute(
            "UPDATE execution_authorities SET claim_state='unknown' WHERE claim_id=?",
            (str(current.claim_id),),
        )
    # Cleanup facts alone cannot release admission before completion/output integration.
    self._commit_write(connection, fd)
