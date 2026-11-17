"""Synthetic launch, completion and stop DTO helpers; no Docker or engine queries."""

import time
from pathlib import Path
from uuid import UUID, uuid4

from jeff.application.execution import (
    DockerCLIIdentity,
    DockerCompletion,
    DockerJobOutcome,
    DockerLaunchEvent,
    DockerLaunchIntent,
    DockerLaunchScope,
    DockerOwnedContainer,
    ExecutionInspectionScope,
    ExecutionManifest,
    ExecutionStopCapture,
    ExecutionStopObservation,
    ExecutionStopRecord,
    ExecutionStopRequest,
    ExecutionStopTerminal,
    PreparedInputBinding,
    VerifiedDockerTarget,
)
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_output import GuardedOutputCollector
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import command


def launch_intent(
    store: SQLiteStorage, project: Project, manifest: ExecutionManifest, generation: UUID
) -> DockerLaunchIntent:
    record = store.authority(
        command(
            project,
            generation,
            operation="propose",
            manifest=manifest,
            current_manifest_digest=manifest.fingerprint,
        ),
        deadline=time.monotonic() + 5,
    ).record
    store.authority(
        command(
            project,
            generation,
            operation="approve",
            approval_id=record.approval_id,
            current_manifest_digest=manifest.fingerprint,
        ),
        deadline=time.monotonic() + 5,
    )
    claimed = store.authority(
        command(
            project,
            generation,
            operation="claim",
            approval_id=record.approval_id,
            current_manifest_digest=manifest.fingerprint,
        ),
        deadline=time.monotonic() + 5,
    ).record
    profile = manifest.profile
    target = VerifiedDockerTarget(
        declaration_digest="a" * 64,
        profile_digest=profile.fingerprint,
        binary=DockerCLIIdentity(path=Path("/synthetic/docker"), sha256="b" * 64, size=1),
        endpoint=profile.endpoint,
        engine_id=profile.engine_id,
        image_id=profile.image_id,
        platform=profile.platform,
        client_version="synthetic",
        server_version="synthetic",
        image_configuration_digest="c" * 64,
        effective_environment=(),
    )
    return DockerLaunchIntent(
        attempt_id=uuid4(),
        record=claimed,
        target=target,
        inputs=PreparedInputBinding(
            snapshot_id=manifest.inputs.snapshot_id,
            inventory_digest=manifest.inputs.digest,
            path=project.root.parent / "prepared",
        ),
        nonce="d" * 32,
    )


def launch_scope(intent: DockerLaunchIntent) -> DockerLaunchScope:
    assert intent.record.claim_id is not None
    return DockerLaunchScope(
        project_id=intent.record.project_id,
        canonical_root=intent.record.canonical_root,
        claim_id=intent.record.claim_id,
    )


def fact(intent: DockerLaunchIntent, phase: str) -> DockerLaunchEvent:
    return DockerLaunchEvent.model_validate(
        {
            "intent": intent,
            "phase": phase,
            **(
                {"container_id": "e" * 64, "created_at": "2026-01-01T00:00:00.000000000Z"}
                if phase not in {"create_intent", "unknown"}
                else {}
            ),
            **({"exit_code": 7} if phase == "exited" else {}),
        }
    )


def completion(
    intent: DockerLaunchIntent,
    text: bytes = b"public output\n",
    *,
    unknown: bool = False,
    exit_code: int = 7,
) -> DockerCompletion:
    capture = GuardedOutputCollector(intent.record.manifest.profile.output_bytes)
    for offset in range(0, len(text), 16384):
        capture.accept("stdout", text[offset : offset + 16384])
    capture.finish("stdout")
    capture.finish("stderr")
    return DockerCompletion(
        owned=DockerOwnedContainer(
            intent=intent, container_id="e" * 64, created_at="2026-01-01T00:00:00.000000000Z"
        ),
        outcome=DockerJobOutcome(
            exit_code=exit_code, cleanup_verified=True, outcome_unknown=unknown
        ),
        output=capture.seal(),
    )


def stopped_sample(
    store: SQLiteStorage,
    project: Project,
    manifest: ExecutionManifest,
    generation: UUID,
    *,
    retain: bool = True,
) -> tuple[ExecutionStopRecord, ExecutionStopCapture]:
    from jeff.domain.execution import fingerprint

    intent = launch_intent(store, project, manifest, generation)
    for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
        store.record(fact(intent, phase), deadline=time.monotonic() + 5)
    scope = ExecutionInspectionScope(
        project_id=project.project_id,
        canonical_root=project.root,
        owner_instance_id=generation,
        approval_id=intent.record.approval_id,
    )
    request = ExecutionStopRequest(
        **scope.model_dump(), request_id=uuid4(), manifest_digest=manifest.fingerprint
    )
    record = store.request_stop(request, deadline=time.monotonic() + 5)
    capture = GuardedOutputCollector(manifest.profile.output_bytes)
    capture.accept("stdout", b"safe prefix\n")
    digest_helper = completion(intent).model_copy(update={"output": capture.seal(abort=True)})
    owned = digest_helper.owned
    observation = ExecutionStopObservation(
        scope=scope,
        request_id=record.request_id,
        manifest_digest=manifest.fingerprint,
        owned=owned,
        terminal=ExecutionStopTerminal(
            outcome="cancelled",
            evidence="owned",
            cleanup_verified=True,
            exit_code=7,
            attempt_id=intent.attempt_id,
            ownership_digest=fingerprint(
                {
                    "intent": intent.fingerprint,
                    "container": owned.container_id,
                    "birth": owned.created_at,
                }
            ),
            output_facts=digest_helper.output_facts,
            output_digest=digest_helper.output_digest,
        ),
    )
    sample = ExecutionStopCapture(observation=observation, output=digest_helper.output)
    final = (
        store.finish_stop_with_capture(observation, sample.output, deadline=time.monotonic() + 5)
        if retain
        else (store.finish_stop(observation, deadline=time.monotonic() + 5))
    )
    return final, sample
