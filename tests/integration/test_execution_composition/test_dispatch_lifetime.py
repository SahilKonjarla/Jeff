"""Existing worker ownership and one absolute shutdown budget."""

import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.dispatch import DispatchReply, ProjectCall, ProjectDispatcher
from jeff.application.questions import QuestionService
from jeff.application.runtime import ControlFault, RuntimeIdentityData
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.integrations.git import GitRepositoryRead

from .support_composition import approved, composed, show


def test_dispatcher_owns_admitted_job_until_completion_without_an_execution_queue(
    tmp_path: Path,
) -> None:
    from jeff.application.execution import (
        ExecutionStartCall,
        ExecutionStartReply,
        FreshExecutionCall,
    )

    with composed(tmp_path) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        assert store.instance_id is not None
        identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
        workers = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            ),
            starter=coordinator,
        )
        docker.entered = threading.Event()
        docker.release = threading.Event()
        workers.start()
        try:
            call = ExecutionStartCall(
                directory=project.root,
                approval_id=record.approval_id,
                reviewed_manifest_digest=record.manifest_digest,
            )
            acknowledgement = workers.submit(call, time.monotonic() + 5)
            reply = acknowledgement.result(timeout=2)
            assert (
                isinstance(reply, ExecutionStartReply)
                and reply.admission is not None
                and reply.error is None
            )
            assert reply.request_id == call.request_id and reply.identity == identity.instance_id
            with pytest.raises(ValidationError):
                ExecutionStartReply(
                    request_id=uuid4(), identity=identity.instance_id, admission=reply.admission
                )
            with pytest.raises(ValidationError):
                ExecutionStartReply(request_id=call.request_id, identity=identity.instance_id)
            with pytest.raises(ValidationError):
                ExecutionStartCall.model_validate(
                    {**call.model_dump(), "current_manifest_digest": "a" * 64}
                )
            with pytest.raises(ValidationError):
                ExecutionStartCall.model_validate(
                    {**call.model_dump(), "authority_kind": "review_only_v1"}
                )
            assert docker.entered.wait(2) and not workers.actions_drained
            # Ack completion must not release execution capacity or consume all workers.
            with pytest.raises(ControlFault) as busy:
                workers.submit(call, time.monotonic() + 5)
            assert busy.value.code == "runtime_busy"
            config = workers.submit(
                ProjectCall(directory=project.root, operation="config"), time.monotonic() + 5
            ).result(timeout=2)
            assert isinstance(config, DispatchReply) and config.result.ok
            with pytest.raises(ControlFault) as stopping:
                workers.close(timeout=0)
            assert stopping.value.code == "runtime_stop_unconfirmed" and not workers.closed
            assert not workers.actions_drained
        finally:
            docker.release.set()
            workers.close(timeout=5)
        assert workers.closed and workers.actions_drained
        assert docker.calls == ["create", "attach", "remove"]
        assert show(store, project, record.approval_id).claim_state == "finished"
        assert acknowledgement.result() == reply  # No completion/error overwrites the admission.
        disabled = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            )
        )
        disabled.start()
        try:
            with pytest.raises(ControlFault) as unavailable:
                disabled.submit(call, time.monotonic() + 5)
            assert unavailable.value.code == "runtime_capability_unavailable"
            with pytest.raises(ControlFault) as fresh_unavailable:
                disabled.submit(
                    FreshExecutionCall(
                        request_id=uuid4(),
                        directory=project.root,
                        operation="start",
                        approval_id=record.approval_id,
                        reviewed_manifest_digest=record.manifest_digest,
                    ),
                    time.monotonic() + 5,
                )
            assert fresh_unavailable.value.code == "runtime_capability_unavailable"
        finally:
            disabled.close(timeout=5)
        assert docker.calls == ["create", "attach", "remove"]


def test_shutdown_budget_is_one_absolute_deadline_and_never_success_after_expiry(
    tmp_path: Path,
) -> None:
    from jeff.infrastructure.runtime_ipc import ControlServer

    with composed(tmp_path) as (_, store, _, docker):
        assert store.instance_id is not None
        clock = [100.0]
        server = ControlServer(
            store.owner,
            store.owner.store.read_credential(),
            RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid()),
            store.snapshot,
            monotonic=lambda: clock[0],
        )
        server.request_stop()
        assert server.stop_deadline == 135.0
        clock[0] = 120.0
        server.request_stop()
        assert server.stop_deadline == 135.0
        server.check_stop_budget()
        clock[0] = 135.0
        with pytest.raises(ControlFault) as expired:
            server.check_stop_budget()
        assert expired.value.code == "runtime_stop_unconfirmed"
        assert not docker.calls
