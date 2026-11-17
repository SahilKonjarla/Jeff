"""Authenticated stop/cancel, full ordinary queues and exact-owned cleanup facts."""

import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.dispatch import DispatchReply, ProjectCall, ProjectDispatcher
from jeff.application.questions import QuestionService
from jeff.application.runtime import ControlFault, ControlObservation, RuntimeIdentityData
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.integrations.git import GitRepositoryRead

from .support_composition import approved, composed


@pytest.mark.parametrize("cleanup_confirmed", (True, False))
def test_authenticated_stop_cancels_active_work_without_observer_cancellation(
    tmp_path: Path, cleanup_confirmed: bool
) -> None:
    import asyncio

    from jeff.application.execution import ExecutionInspectionScope, FreshExecutionCall
    from jeff.domain.runtime import RuntimeState
    from jeff.infrastructure.runtime_ipc import ControlClient, ControlServer

    with composed(tmp_path, cancellable=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        assert store.instance_id is not None
        identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
        dispatcher = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            ),
            fresh=coordinator,
        )
        server = ControlServer(
            store.owner, store.owner.store.read_credential(), identity, store.snapshot
        )
        server.dispatcher = dispatcher
        docker.entered = threading.Event()
        dispatcher.start()

        async def exercise() -> None:
            task = asyncio.create_task(server.serve())
            try:
                for _ in range(200):
                    if task.done():
                        await task
                    if store.owner.store.layout.socket.exists():
                        break
                    await asyncio.sleep(0.005)
                ack = dispatcher.submit(
                    FreshExecutionCall(
                        directory=project.root,
                        request_id=uuid4(),
                        operation="start",
                        approval_id=record.approval_id,
                        reviewed_manifest_digest=record.manifest_digest,
                    ),
                    time.monotonic() + 5,
                )
                await asyncio.to_thread(ack.result, 2)
                assert docker.entered is not None
                assert await asyncio.to_thread(docker.entered.wait, 2)

                def stop() -> ControlObservation:
                    with ControlClient(
                        store.owner.store.layout, store.owner.store.read_credential()
                    ).connect(time.monotonic() + 3) as connection:
                        return connection.request("stop", identity.instance_id)

                if cleanup_confirmed:
                    observation = await asyncio.to_thread(stop)
                    assert observation.state == RuntimeState.STOPPING
                else:
                    docker.failure = "cleanup"
                    with pytest.raises(ControlFault) as unconfirmed:
                        await asyncio.to_thread(stop)
                    assert unconfirmed.value.code == "runtime_stop_unconfirmed"
                await asyncio.wait_for(task, 5)
                assert dispatcher.actions_drained and dispatcher.closed
            finally:
                server.request_stop()
                dispatcher.request_close()
                try:
                    await asyncio.wait_for(task, 5)
                finally:
                    dispatcher.close(timeout=5)

        previous_umask = os.umask(0o177)
        try:
            asyncio.run(exercise())
        finally:
            os.umask(previous_umask)
        final = store.inspect_stop(
            ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=identity.instance_id,
                approval_id=record.approval_id,
            ),
            deadline=time.monotonic() + 3,
        )
        assert final is not None and final.reason == "shutdown" and final.terminal is not None
        assert final.terminal.outcome == ("cancelled" if cleanup_confirmed else "unknown")
        assert final.terminal.cleanup_verified == cleanup_confirmed
        assert final.terminal.output_facts is not None and final.terminal.output_facts.aborted
        assert docker.calls == (
            ["create", "attach", "stop", "remove"] if cleanup_confirmed else ["create", "attach"]
        )


def test_authenticated_cancel_bypasses_full_ordinary_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from jeff.application.dispatch import dispatch_failure
    from jeff.application.execution import ExecutionCancelCall, FreshExecutionCall
    from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS
    from jeff.infrastructure.runtime_ipc import ControlServer, RuntimeProjectClient

    with composed(tmp_path, cancellable=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        assert store.instance_id is not None
        identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
        dispatcher = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            ),
            fresh=coordinator,
        )
        server = ControlServer(
            store.owner, store.owner.store.read_credential(), identity, store.snapshot
        )
        server.dispatcher = dispatcher
        client = RuntimeProjectClient(store.owner.store.layout)
        docker.entered = threading.Event()
        release = threading.Event()
        entered = [0]
        guard = threading.Lock()

        def slow(call: ProjectCall, deadline: float) -> DispatchReply:
            with guard:
                entered[0] += 1
            assert release.wait(8)
            return DispatchReply(
                request_id=call.request_id,
                identity=identity,
                result=dispatch_failure("runtime_busy"),
            )

        monkeypatch.setattr(dispatcher.dispatch, "execute", slow)
        dispatcher.start()

        async def exercise() -> None:
            task = asyncio.create_task(server.serve())
            try:
                for _ in range(200):
                    if store.owner.store.layout.socket.exists():
                        break
                    if task.done():
                        await task
                    await asyncio.sleep(0.005)
                started = dispatcher.submit(
                    FreshExecutionCall(
                        request_id=uuid4(),
                        directory=project.root,
                        operation="start",
                        approval_id=record.approval_id,
                        reviewed_manifest_digest=record.manifest_digest,
                    ),
                    time.monotonic() + 5,
                )
                await asyncio.to_thread(started.result, 2)
                assert docker.entered is not None
                assert await asyncio.to_thread(docker.entered.wait, 2)
                for _ in range(3):
                    dispatcher.submit(
                        ProjectCall(request_id=uuid4(), directory=project.root, operation="config"),
                        time.monotonic() + 7,
                    )
                for _ in range(200):
                    with guard:
                        if entered[0] == 3:
                            break
                    await asyncio.sleep(0.005)
                assert entered[0] == 3
                for _ in range(DEFAULT_RUNTIME_LIMITS.pending_requests):
                    dispatcher.submit(
                        ProjectCall(request_id=uuid4(), directory=project.root, operation="config"),
                        time.monotonic() + 7,
                    )
                with pytest.raises(ControlFault):
                    dispatcher.submit(
                        ProjectCall(request_id=uuid4(), directory=project.root, operation="config"),
                        time.monotonic() + 7,
                    )
                call = ExecutionCancelCall(
                    request_id=uuid4(),
                    approval_id=record.approval_id,
                    reviewed_manifest_digest=record.manifest_digest,
                )
                reply = await asyncio.wait_for(asyncio.to_thread(client.cancel, call), 3)
                assert reply.ok and reply.stop is not None and reply.stop.reason == "cancel"
                assert (
                    reply.request_id == call.request_id and reply.identity == identity.instance_id
                )
                assert docker.calls[:2] == ["create", "attach"]
            finally:
                release.set()
                server.request_stop()
                dispatcher.request_close()
                try:
                    await asyncio.wait_for(task, 5)
                finally:
                    # Even a listener/assertion failure must close idle non-daemon workers.
                    dispatcher.close(timeout=5)

        previous_umask = os.umask(0o177)
        try:
            asyncio.run(exercise())
        finally:
            os.umask(previous_umask)
        assert docker.calls == ["create", "attach", "stop", "remove"]
