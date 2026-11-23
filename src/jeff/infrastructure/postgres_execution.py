"""Proof-only M03 authority and launch journal; never a Docker controller."""

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    DockerBackendFault,
    DockerLaunchEvent,
    DockerLaunchScope,
    DockerLaunchSnapshot,
    GuardedExecutionManifest,
    ReviewFault,
)
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState, fingerprint

if TYPE_CHECKING:
    from .postgres_storage import Connection

EXECUTION_DDL = (
    "CREATE TABLE jeff_proof.authorities (approval_id uuid PRIMARY KEY, "
    "claim_id uuid UNIQUE, payload jsonb NOT NULL)",
    "CREATE TABLE jeff_proof.authority_ops (request_id uuid PRIMARY KEY, digest text NOT NULL, "
    "approval_id uuid NOT NULL REFERENCES jeff_proof.authorities(approval_id))",
    "CREATE TABLE jeff_proof.launches (attempt_id uuid PRIMARY KEY, "
    "claim_id uuid UNIQUE NOT NULL REFERENCES jeff_proof.authorities(claim_id), "
    "intent_digest text NOT NULL, snapshot jsonb NOT NULL)",
    "CREATE TABLE jeff_proof.launch_events (attempt_id uuid NOT NULL "
    "REFERENCES jeff_proof.launches(attempt_id), phase text NOT NULL, digest text NOT NULL, "
    "PRIMARY KEY (attempt_id,phase))",
)
TTL_MS = 600_000


def find_authority(
    connection: "Connection", key: UUID, *, by_claim: bool = False
) -> AuthorityRecord:
    column = "claim_id" if by_claim else "approval_id"
    row = connection.execute(
        f"SELECT payload FROM jeff_proof.authorities WHERE {column}=%s", (key,)
    ).fetchone()
    if row is None:
        raise ReviewFault("execution_not_found")
    record = AuthorityRecord.model_validate(row[0])
    if (record.claim_id if by_claim else record.approval_id) != key:
        raise StorageFault("storage_unrecognized")
    return record


def _registered(connection: "Connection", project: UUID, root: Path) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM jeff_proof.projects WHERE project_id=%s AND canonical_root=%s",
            (project, str(root)),
        ).fetchone()
        is not None
    )


def save_authority(connection: "Connection", record: AuthorityRecord) -> None:
    connection.execute(
        "UPDATE jeff_proof.authorities SET claim_id=%s,payload=%s::jsonb WHERE approval_id=%s",
        (record.claim_id, record.model_dump_json(), record.approval_id),
    )


def decide(
    connection: "Connection",
    operation: AuthorityOperation,
    owner: UUID,
    now: int,
    *,
    live_proposals: set[UUID],
) -> tuple[AuthorityRecord, bool]:
    if operation.owner_instance_id != owner or not _registered(
        connection, operation.project_id, operation.canonical_root
    ):
        raise ReviewFault("execution_target_mismatch")
    digest = fingerprint(operation.model_dump(mode="json"))
    if operation.operation != "show":
        prior = connection.execute(
            "SELECT digest, approval_id FROM jeff_proof.authority_ops WHERE request_id=%s",
            (operation.request_id,),
        ).fetchone()
        if prior is not None:
            if prior[0] != digest or not isinstance(prior[1], UUID):
                raise ReviewFault("execution_conflict")
            return find_authority(connection, prior[1]), False  # Replay is facts only.
    if operation.operation == "propose":
        manifest = operation.manifest
        if (
            not isinstance(manifest, GuardedExecutionManifest)
            or manifest.profile.project_id != operation.project_id
            or manifest.profile.canonical_root != operation.canonical_root
            or manifest.fingerprint != operation.current_manifest_digest
        ):
            raise ReviewFault("execution_target_mismatch")
        record = AuthorityRecord(
            proposal_id=uuid4(),
            approval_id=uuid4(),
            job_id=uuid4(),
            project_id=operation.project_id,
            canonical_root=operation.canonical_root,
            owner_instance_id=owner,
            manifest=manifest,
            manifest_digest=manifest.fingerprint,
            state=AuthorityState.PENDING,
            created_at_ms=now,
            expires_at_ms=now + TTL_MS,
        )
        connection.execute(
            "INSERT INTO jeff_proof.authorities VALUES (%s,NULL,%s::jsonb)",
            (record.approval_id, record.model_dump_json()),
        )
    else:
        assert operation.approval_id is not None
        record = find_authority(connection, operation.approval_id)
        if (record.project_id, record.canonical_root) != (
            operation.project_id,
            operation.canonical_root,
        ):
            raise ReviewFault("execution_not_found")
        if operation.operation == "show":
            return record, False
        needed = (
            AuthorityState.APPROVED if operation.operation == "claim" else AuthorityState.PENDING
        )
        if record.state != needed:
            raise ReviewFault("execution_conflict")
        if (
            record.owner_instance_id != owner
            or record.approval_id not in live_proposals
            or not record.created_at_ms <= now < record.expires_at_ms
        ):
            raise ReviewFault("execution_expired")
        if operation.operation in {"approve", "claim"} and (
            operation.current_manifest_digest != record.manifest_digest
        ):
            raise ReviewFault("execution_target_mismatch")
        if operation.operation == "claim":
            if (
                connection.execute(
                    "SELECT 1 FROM jeff_proof.authorities WHERE payload->>'claim_state' "
                    "IN ('claimed','unknown') LIMIT 1"
                ).fetchone()
                is not None
            ):
                raise ReviewFault("execution_outcome_unknown")
            update: dict[str, object] = dict(
                state=AuthorityState.CONSUMED,
                claim_id=uuid4(),
                claim_state="claimed",
                claimed_at_ms=now,
            )
        else:
            update = dict(
                state=AuthorityState.APPROVED
                if operation.operation == "approve"
                else AuthorityState.DENIED
            )
        record = AuthorityRecord.model_validate(record.model_dump() | update)
        save_authority(connection, record)
    connection.execute(
        "INSERT INTO jeff_proof.authority_ops VALUES (%s,%s,%s)",
        (operation.request_id, digest, record.approval_id),
    )
    return record, True


def launch_status(connection: "Connection", scope: DockerLaunchScope) -> DockerLaunchSnapshot:
    record = find_authority(connection, scope.claim_id, by_claim=True)
    if (record.project_id, record.canonical_root) != (scope.project_id, scope.canonical_root):
        raise ReviewFault("execution_not_found")
    row = connection.execute(
        "SELECT snapshot FROM jeff_proof.launches WHERE claim_id=%s", (scope.claim_id,)
    ).fetchone()
    if row is None:
        raise ReviewFault("execution_not_found")
    snapshot = DockerLaunchSnapshot.model_validate(row[0])
    if snapshot.intent.record.claim_id != scope.claim_id:
        raise StorageFault("storage_unrecognized")
    return DockerLaunchSnapshot.model_validate(
        snapshot.model_dump()
        | {
            "claim_state": record.claim_state,
            "uncertain": record.claim_state == "unknown" or "unknown" in snapshot.phases,
        }
    )


def append_launch(
    connection: "Connection",
    event: DockerLaunchEvent,
    owner: UUID,
    confirmed: set[UUID],
    *,
    allow_mutation: bool,
) -> None:
    intent = event.intent
    claim = intent.record.claim_id
    if claim is None or claim not in confirmed or intent.record.owner_instance_id != owner:
        raise DockerBackendFault("docker_authority_required")
    current = find_authority(connection, claim, by_claim=True)
    if current.model_dump(exclude={"claim_state"}) != intent.record.model_dump(
        exclude={"claim_state"}
    ):
        raise DockerBackendFault("docker_authority_required")
    stop = connection.execute(
        "SELECT 1 FROM jeff_proof.stops WHERE approval_id=%s", (current.approval_id,)
    ).fetchone()
    if current.claim_state == "finished" or (
        event.phase in {"create_intent", "start_intent"}
        and (not allow_mutation or stop is not None)
    ):
        raise DockerBackendFault("docker_authority_required")
    row = connection.execute(
        "SELECT attempt_id,intent_digest,snapshot FROM jeff_proof.launches WHERE claim_id=%s",
        (claim,),
    ).fetchone()
    digest = fingerprint(
        {"intent": intent.fingerprint, **event.model_dump(mode="json", exclude={"intent"})}
    )
    if row is None:
        if event.phase != "create_intent" or current.claim_state != "claimed":
            raise DockerBackendFault("docker_authority_required")
        snapshot = DockerLaunchSnapshot(
            intent=intent, phases=(), claim_state="claimed", uncertain=False
        )
        connection.execute(
            "INSERT INTO jeff_proof.launches VALUES (%s,%s,%s,%s::jsonb)",
            (intent.attempt_id, claim, intent.fingerprint, snapshot.model_dump_json()),
        )
    else:
        if row[:2] != (intent.attempt_id, intent.fingerprint):
            raise DockerBackendFault("docker_authority_required")
        snapshot = DockerLaunchSnapshot.model_validate(row[2])
    phase = event.phase
    if phase in snapshot.phases:
        prior = connection.execute(
            "SELECT digest FROM jeff_proof.launch_events WHERE attempt_id=%s AND phase=%s",
            (intent.attempt_id, phase),
        ).fetchone()
        if phase in {"create_intent", "start_intent"} or prior != (digest,):
            raise DockerBackendFault("docker_mutation_unconfirmed")
        return
    order = ("create_intent", "created", "start_intent", "exited", "cleaned")
    stopped_before_start = (
        stop is not None and phase == "cleaned" and snapshot.phases == ("create_intent", "created")
    )
    if "unknown" in snapshot.phases or (
        phase != "unknown"
        and not stopped_before_start
        and (len(snapshot.phases) >= len(order) or phase != order[len(snapshot.phases)])
    ):
        raise DockerBackendFault("docker_authority_required")
    fields = ("container_id", "created_at", "observed_container_id", "exit_code")
    update = {}
    for name in fields:
        old, new = getattr(snapshot, name), getattr(event, name)
        if old is not None and new is not None and new != old:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        update[name] = old if old is not None else new
    if update["container_id"] is not None and update["observed_container_id"] not in (
        None,
        update["container_id"],
    ):
        raise DockerBackendFault("docker_ownership_unconfirmed")
    if phase == "unknown":
        current = AuthorityRecord.model_validate(current.model_dump() | {"claim_state": "unknown"})
        save_authority(connection, current)
    new_snapshot = DockerLaunchSnapshot.model_validate(
        snapshot.model_dump()
        | update
        | {
            "phases": (*snapshot.phases, phase),
            "claim_state": current.claim_state,
            "uncertain": current.claim_state == "unknown",
        }
    )
    connection.execute(
        "UPDATE jeff_proof.launches SET snapshot=%s::jsonb WHERE attempt_id=%s",
        (new_snapshot.model_dump_json(), intent.attempt_id),
    )
    connection.execute(
        "INSERT INTO jeff_proof.launch_events VALUES (%s,%s,%s)", (intent.attempt_id, phase, digest)
    )


def fence(connection: "Connection", prior_owner: UUID) -> int:
    rows = connection.execute(
        "SELECT payload FROM jeff_proof.authorities WHERE payload->>'owner_instance_id'=%s "
        "AND (payload->>'state' IN ('pending','approved') OR payload->>'claim_state'='claimed') "
        "ORDER BY approval_id LIMIT 100",
        (str(prior_owner),),
    ).fetchall()
    for (payload,) in rows:
        record = AuthorityRecord.model_validate(payload)
        change = (
            dict(claim_state="unknown")
            if record.claim_state == "claimed"
            else dict(state=AuthorityState.INVALIDATED)
        )
        save_authority(connection, AuthorityRecord.model_validate(record.model_dump() | change))
    return len(rows)
