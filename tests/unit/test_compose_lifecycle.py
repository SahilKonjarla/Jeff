"""Pure lifecycle facts do not broaden review or authorize replay."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.compose_lifecycle import (
    ResourceIdentity,
    ServiceAttempt,
    ServiceEvent,
    ServiceResources,
    ServiceStart,
    ServiceStop,
    ServiceTarget,
    append_service_event,
    stop_matches_start,
)


def _start() -> ServiceStart:
    return ServiceStart(
        target=ServiceTarget(
            project_id=uuid4(),
            root=Path("/fixture"),
            project="sandbox-project",
            service="api",
            engine_id="proof-engine",
            endpoint="unix:///fixture/docker.sock",
        ),
        configuration_digest="a" * 64,
        image_id="sha256:" + "b" * 64,
        platform="linux/arm64",
        memory_bytes=128 * 1024 * 1024,
        nano_cpus=1_000_000_000,
        pids_limit=32,
    )


def _attempt(manifest: ServiceStart | ServiceStop, reviewed: str | None = None) -> ServiceAttempt:
    return ServiceAttempt(
        manifest=manifest, reviewed_digest=reviewed or manifest.digest,
        job_id=uuid4(), claim_id=uuid4(), worker_instance_id=uuid4(),
    )


def _resources(container_id: str = "c" * 64) -> ServiceResources:
    return ServiceResources(
        container=ResourceIdentity(id=container_id, created_at="2025-11-23T12:00:00Z"),
        network=ResourceIdentity(id="d" * 64, created_at="2025-11-23T11:59:59Z"),
    )


def _running(start: ServiceAttempt, resources: ServiceResources) -> tuple[ServiceEvent, ...]:
    history = append_service_event(start, (), ServiceEvent(phase="intent"))
    history = append_service_event(start, history, ServiceEvent(phase="created", resources=resources))
    return append_service_event(start, history, ServiceEvent(phase="running", resources=resources))


def _stop(start: ServiceAttempt, resources: ServiceResources) -> ServiceStop:
    return ServiceStop(
        target=start.manifest.target, start_job_id=start.job_id,
        start_manifest_digest=start.reviewed_digest, resources=resources,
    )


def test_start_review_digest_covers_target_image_network_and_limits() -> None:
    start = _start()
    original = start.model_dump(mode="json")
    assert ServiceStart.model_validate(original).digest == start.digest
    for changed in (
        {"target": original["target"] | {"engine_id": "other-engine"}},
        {"image_id": "sha256:" + "a" * 64},
        {"configuration_digest": "c" * 64},
        {"pids_limit": 64},
    ):
        manifest = ServiceStart.model_validate(original | changed)
        with pytest.raises(ValueError, match="does not match"):
            append_service_event(_attempt(manifest, start.digest), (), ServiceEvent(phase="intent"))
    for changed in (
        {"docker_published_ports": True}, {"external_egress": True}, {"network": "bridge"},
        {"health_relay": {"host": "0.0.0.0"}}, {"health_relay": {"port": 41000}},
        {"health_relay": {"path": "/other"}}, {"health_relay": {"method": "POST"}},
        {"health_relay": {"container_port": 9000}},
    ):
        with pytest.raises(ValidationError):
            ServiceStart.model_validate(original | changed)


def test_start_requires_exact_resources_and_separate_stop_review() -> None:
    start = _attempt(_start())
    resources = _resources()
    history = _running(start, resources)
    stop = _stop(start, resources)
    assert stop.digest != start.reviewed_digest
    assert stop_matches_start(stop, start, history)
    assert not stop_matches_start(stop.model_copy(update={"start_job_id": uuid4()}), start, history)
    assert not stop_matches_start(_stop(start, _resources("e" * 64)), start, history)
    wrong_target = stop.target.model_copy(update={"engine_id": "different-engine"})
    assert not stop_matches_start(stop.model_copy(update={"target": wrong_target}), start, history)
    stop_attempt = _attempt(stop)
    assert stop_attempt.worker_instance_id != start.worker_instance_id
    stopped = append_service_event(stop_attempt, (), ServiceEvent(phase="intent"))
    with pytest.raises(ValueError, match="exact resource"):
        append_service_event(
            stop_attempt, stopped, ServiceEvent(phase="stopped", resources=_resources("e" * 64))
        )
    assert append_service_event(
        stop_attempt, stopped, ServiceEvent(phase="stopped", resources=resources)
    )[-1].phase == "stopped"


def test_unconfirmed_start_is_terminal_and_cannot_establish_stop_provenance() -> None:
    start = _attempt(_start())
    intent = append_service_event(start, (), ServiceEvent(phase="intent"))
    with pytest.raises(ValueError, match="exact observed"):
        append_service_event(start, intent, ServiceEvent(phase="created"))
    created = append_service_event(start, intent, ServiceEvent(phase="created", resources=_resources()))
    uncertain = append_service_event(start, created, ServiceEvent(phase="unknown"))
    assert not stop_matches_start(_stop(start, _resources()), start, uncertain)
    early = append_service_event(start, intent, ServiceEvent(phase="unknown"))
    with pytest.raises(ValueError, match="do not replay"):
        append_service_event(start, early, ServiceEvent(phase="created", resources=_resources()))


def test_durable_resources_reject_relay_address() -> None:
    with pytest.raises(ValidationError):
        ServiceResources.model_validate(_resources().model_dump() | {"host_port": 53129})


def test_birth_evidence_cannot_drift() -> None:
    start = _attempt(_start())
    intent = (ServiceEvent(phase="intent"),)
    actual = _resources()
    created = append_service_event(start, intent, ServiceEvent(phase="created", resources=actual))
    replacement = actual.model_copy(update={"container": ResourceIdentity(
        id=actual.container.id, created_at="2025-11-23T13:00:00Z"
    )})
    with pytest.raises(ValueError, match="exact resource"):
        append_service_event(start, created, ServiceEvent(phase="running", resources=replacement))
