"""Use the real dispatcher and service orchestration with no Docker or database."""

import time
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from jeff.application.compose_lifecycle import (
    ServiceAction,
    ServiceAttempt,
    ServiceAuthorityOperation,
    ServiceAuthorityRecord,
    ServiceAuthorityResult,
    ServiceEvent,
    ServiceResources,
    ServiceStart,
    ServiceStop,
    append_service_event,
)
from jeff.application.compose_service_worker import (
    HealthRelayEndpoint,
    ServiceWork,
    ServiceWorker,
    ServiceWorkResult,
)
from jeff.application.dispatch import ProjectDispatcher
from jeff.application.runtime import ControlFault
from jeff.domain.execution import AuthorityState
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher

from test_compose_lifecycle import _resources, _start, _stop


@pytest.mark.parametrize(
    "fault", [None, "claim_ack", "intent_ack", "create", "created_ack", "terminal_ack", "relay"]
)
def test_service_work_waits_for_claim_and_journal_acknowledgements(fault: str | None) -> None:
    owner = uuid4()
    start = _start()
    resources = _resources()
    created = resources
    trace: list[str] = []
    approved = ServiceAuthorityRecord(
        proposal_id=uuid4(),
        approval_id=uuid4(),
        job_id=uuid4(),
        project_id=start.target.project_id,
        canonical_root=start.target.root,
        owner_instance_id=owner,
        manifest=start,
        manifest_digest=start.digest,
        state=AuthorityState.APPROVED,
        created_at_ms=1,
        expires_at_ms=60_000,
    )
    record = approved
    history: tuple[ServiceEvent, ...] = ()
    launched: ServiceAttempt | None = None

    class Journal:
        def service_authority(
            self, operation: ServiceAuthorityOperation, *, deadline: float
        ) -> ServiceAuthorityResult:
            nonlocal record
            trace.append(operation.operation)
            assert operation.approval_id == record.approval_id and deadline > time.monotonic()
            if operation.operation == "show":
                return ServiceAuthorityResult(record=record)
            assert operation.operation == "claim"
            assert operation.current_manifest_digest == record.manifest_digest
            claim = uuid4()
            record = ServiceAuthorityRecord.model_validate(
                record.model_dump()
                | dict(state=AuthorityState.CONSUMED, claim_id=claim, claim_state="claimed", claimed_at_ms=2)
            )
            if fault == "claim_ack":
                raise RuntimeError("claim reply loss")
            return ServiceAuthorityResult(record=record, launch_claim=claim)

        def record_service(
            self, attempt: ServiceAttempt, event: ServiceEvent, *, deadline: float
        ) -> None:
            nonlocal history, launched
            trace.append(f"journal:{event.phase}")
            assert deadline > time.monotonic() and record.claim_id == attempt.claim_id
            history = append_service_event(attempt, history, event)
            if event.phase == "running":
                launched = attempt
            if fault == f"{event.phase}_ack" or (fault == "terminal_ack" and event.phase == "running"):
                raise RuntimeError("journal reply loss")

    class Backend:
        def verify(self, manifest: ServiceAction, *, deadline: float) -> str:
            trace.append("verify")
            return manifest.digest

        def create(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
            trace.append("create")
            assert isinstance(attempt.manifest, ServiceStart) and history[-1].phase == "intent"
            if fault == "create":
                raise RuntimeError("ambiguous create")
            return created

        def start(
            self, attempt: ServiceAttempt, observed: ServiceResources, *, deadline: float
        ) -> ServiceResources:
            trace.append("start")
            assert isinstance(attempt.manifest, ServiceStart)
            assert history[-1] == ServiceEvent(phase="created", resources=observed)
            assert observed == created
            return resources

        def restart(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
            raise AssertionError("No restart is approved in this test")

        def stop(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
            trace.append("stop")
            assert isinstance(attempt.manifest, ServiceStop) and history[-1].phase == "intent"
            assert attempt.manifest.resources == resources
            return resources

    class Relays:
        def open(
            self, attempt: ServiceAttempt, observed: ServiceResources, *, deadline: float
        ) -> HealthRelayEndpoint:
            assert history[-1].phase == "running" and observed == resources
            trace.append("relay:open")
            if fault == "relay":
                raise OSError("relay bind refused")
            return HealthRelayEndpoint(owner_instance_id=owner, port=53129)

        def close(self, start_job_id: UUID, *, deadline: float) -> None:
            assert launched is not None and start_job_id == launched.job_id
            assert history[-1].phase == "intent"
            trace.append("relay:close")

    dispatch = cast(ProjectDispatcher, SimpleNamespace())
    relays = Relays()
    queue = RuntimeDispatcher(
        dispatch, service_worker=ServiceWorker(Journal(), Backend(), owner, relays=relays)
    )

    def submit() -> ServiceWorkResult:
        call = ServiceWork(
            request_id=uuid4(), project_id=record.project_id, canonical_root=record.canonical_root,
            approval_id=record.approval_id, reviewed_digest=record.manifest_digest,
        )
        response = queue.submit(call, time.monotonic() + 5).result(5)
        assert isinstance(response, ServiceWorkResult) and response.request_id == call.request_id
        return response

    queue.start()
    try:
        result = submit()
    finally:
        queue.close(timeout=5)
    prefix = ["show", "verify", "claim"]
    expected = {
        "claim_ack": [],
        "intent_ack": ["journal:intent"],
        "create": ["journal:intent", "create", "journal:unknown"],
        "created_ack": ["journal:intent", "create", "journal:created"],
        "terminal_ack": ["journal:intent", "create", "journal:created", "start", "journal:running"],
    }
    if fault is not None and fault != "relay":
        assert trace == prefix + expected[fault]
        assert result.outcome == "unknown" and result.terminal_recorded == (fault == "create")
        assert result.health_relay is None
        return
    assert trace == prefix + expected["terminal_ack"] + ["relay:open"]
    assert result.outcome == "running" and result.terminal_recorded and launched is not None
    if fault == "relay":
        assert result.health_relay is None
        return
    assert result.health_relay == HealthRelayEndpoint(owner_instance_id=owner, port=53129)
    assert "53129" not in history[-1].model_dump_json()

    # Independent worker turn and fresh stop approval; no restart or remove backend exists.
    stop = _stop(launched, resources)
    record = ServiceAuthorityRecord.model_validate(
        approved.model_dump()
        | dict(proposal_id=uuid4(), approval_id=uuid4(), job_id=uuid4(), manifest=stop,
               manifest_digest=stop.digest)
    )
    history, trace = (), []
    queue = RuntimeDispatcher(
        dispatch, service_worker=ServiceWorker(Journal(), Backend(), owner, relays=relays)
    )
    queue.start()
    try:
        stopped = submit()
    finally:
        queue.close(timeout=5)
    assert stopped.outcome == "stopped" and stopped.terminal_recorded
    assert stopped.health_relay is None
    assert trace == prefix + ["journal:intent", "relay:close", "stop", "journal:stopped"]


def test_default_dispatcher_does_not_enable_service_jobs() -> None:
    dispatch = cast(ProjectDispatcher, SimpleNamespace())
    queue = RuntimeDispatcher(dispatch)
    target = _start()
    call = ServiceWork(
        request_id=uuid4(), project_id=target.target.project_id, canonical_root=target.target.root,
        approval_id=uuid4(), reviewed_digest=target.digest,
    )
    queue.start()
    try:
        with pytest.raises(ControlFault) as error:
            queue.submit(call, time.monotonic() + 5)
        assert error.value.code == "runtime_capability_unavailable"
    finally:
        queue.close(timeout=5)
