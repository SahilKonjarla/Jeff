"""Authenticated fresh execution caller disconnects and protected preparation modes."""

import os
import threading
import time
from pathlib import Path
from typing import NoReturn, cast
from uuid import uuid4

import pytest

from jeff.application.dispatch import ProjectDispatcher
from jeff.application.execution import PreparedInputFault
from jeff.application.questions import QuestionService
from jeff.application.runtime import RuntimeIdentityData
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.integrations.git import GitRepositoryRead

from .support_composition import composed


def test_fresh_authenticated_rpc_keeps_owned_job_after_caller_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from typer.testing import CliRunner

    from jeff.application.dispatch import FreshExecutionRequest
    from jeff.application.execution import FreshExecutionCall, FreshExecutionReply
    from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS
    from jeff.infrastructure.runtime_ipc import (
        ControlClient,
        ControlServer,
        RuntimeProjectClient,
        encode_message,
    )
    from jeff.interfaces.cli import create_app

    with composed(tmp_path) as (coordinator, store, project, docker):
        assert store.instance_id is not None
        identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
        workers = RuntimeDispatcher(
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
        server.dispatcher = workers
        client = RuntimeProjectClient(store.owner.store.layout)
        monkeypatch.chdir(project.root)

        def forbidden() -> NoReturn:
            raise AssertionError("unrelated CLI factory invoked")

        app = create_app(forbidden, forbidden, fresh_execution=lambda: client)
        runner = CliRunner()
        docker.entered = threading.Event()
        docker.release = threading.Event()
        workers.start()

        async def exercise() -> None:
            assert docker.entered is not None and docker.release is not None
            task = asyncio.create_task(server.serve())
            try:
                for _ in range(200):
                    if store.owner.store.layout.socket.exists():
                        break
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)

                def invoke(operation: str, **fields: object) -> FreshExecutionReply:
                    arguments = ["execution", operation]
                    if "approval_id" in fields:
                        arguments.append(str(fields["approval_id"]))
                    if "reviewed_manifest_digest" in fields:
                        arguments.extend(["--digest", str(fields["reviewed_manifest_digest"])])
                    if "profile_name" in fields:
                        arguments.extend(["--profile", str(fields["profile_name"])])
                    for path in cast(tuple[str, ...], fields.get("paths", ())):
                        arguments.extend(["--input", path])
                    arguments.append("--json")
                    if "argv" in fields:
                        arguments.extend(["--", *cast(tuple[str, ...], fields["argv"])])
                    response = runner.invoke(app, arguments)
                    assert response.exit_code in (0, 1), response.output
                    return FreshExecutionReply.model_validate_json(response.stdout)

                proposal = await asyncio.to_thread(
                    invoke,
                    "propose",
                    profile_name="python",
                    argv=("/usr/local/bin/python", "-V"),
                    paths=("source.py",),
                )
                assert proposal.ok and proposal.view is not None
                target = {
                    "approval_id": proposal.view.approval_id,
                    "reviewed_manifest_digest": proposal.view.manifest_digest,
                }
                assert (await asyncio.to_thread(invoke, "approve", **target)).ok
                ack = await asyncio.to_thread(invoke, "start", **target)
                assert (
                    ack.ok and ack.admission is not None and ack.admission.claim_state == "claimed"
                )
                # Unary client has closed its authenticated connection; worker still owns the job.
                assert await asyncio.to_thread(docker.entered.wait, 2)
                assert not workers.actions_drained
                busy = await asyncio.to_thread(invoke, "start", **target)
                assert not busy.ok
                during = await asyncio.to_thread(
                    invoke, "show", approval_id=proposal.view.approval_id
                )
                assert (
                    during.ok and during.view is not None and during.view.claim_state == "claimed"
                )
                assert (
                    during.completion is None and during.completion_state == "launch_without_result"
                )
                docker.release.set()
                for _ in range(200):
                    if workers.actions_drained:
                        break
                    await asyncio.sleep(0.01)
                result = await asyncio.to_thread(
                    invoke, "logs", approval_id=proposal.view.approval_id
                )
                assert (
                    result.ok and result.view is not None and result.view.claim_state == "finished"
                )
                assert result.output is not None and result.output.stdout == "public output\n"
                # A second synthetic job loses its caller before acknowledgement delivery.
                second = await asyncio.to_thread(
                    invoke,
                    "propose",
                    profile_name="python",
                    argv=("/usr/local/bin/python", "-V"),
                    paths=("source.py",),
                )
                assert second.view is not None
                second_target = {
                    "approval_id": second.view.approval_id,
                    "reviewed_manifest_digest": second.view.manifest_digest,
                }
                assert (await asyncio.to_thread(invoke, "approve", **second_target)).ok
                docker.entered.clear()
                docker.release.clear()
                lost_call = FreshExecutionCall(
                    request_id=uuid4(),
                    directory=project.root,
                    operation="start",
                    approval_id=second.view.approval_id,
                    reviewed_manifest_digest=second.view.manifest_digest,
                )

                def send_then_disconnect() -> None:
                    with ControlClient(
                        store.owner.store.layout, store.owner.store.read_credential()
                    ).connect(time.monotonic() + 5) as connection:
                        frame = FreshExecutionRequest(
                            call=lost_call,
                            expected_instance_id=connection.identity.instance_id,
                            remaining_ms=5000,
                        )
                        connection.connection.sendall(
                            encode_message(
                                frame.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes
                            )
                        )

                await asyncio.to_thread(send_then_disconnect)
                assert await asyncio.to_thread(docker.entered.wait, 2)
                assert not workers.actions_drained
                docker.release.set()
                for _ in range(200):
                    if workers.actions_drained:
                        break
                    await asyncio.sleep(0.01)
                recovered = await asyncio.to_thread(
                    invoke, "show", approval_id=second.view.approval_id
                )
                assert (
                    recovered.ok
                    and recovered.view is not None
                    and recovered.view.claim_state == "finished"
                )
                duplicate = await asyncio.to_thread(client.fresh, lost_call)
                assert duplicate.admission is not None and duplicate.admission.replayed
                assert duplicate.admission.claim_state == "finished"
            finally:
                docker.release.set()
                server.request_stop()
                await asyncio.wait_for(task, 5)

        previous_umask = os.umask(0o177)
        try:
            asyncio.run(exercise())
        finally:
            try:
                docker.release.set()
                workers.close(timeout=5)
            finally:
                os.umask(previous_umask)
        assert docker.calls == ["create", "attach", "remove"] * 2 and workers.actions_drained


def test_proposal_preparation_preserves_private_modes_under_daemon_umask(tmp_path: Path) -> None:
    for index, mask in enumerate((0o077, 0o177)):
        with composed(tmp_path / str(index)) as (coordinator, store, project, docker):
            (project.root / "nested").mkdir(mode=0o700)
            (project.root / "nested" / "part.py").write_text("nested public\n")
            old = os.umask(mask)
            try:
                proposal = coordinator.propose(
                    project.root,
                    "python",
                    ("/usr/local/bin/python", "-V"),
                    ("source.py", "nested/part.py"),
                    request_id=uuid4(),
                    deadline=time.monotonic() + 5,
                )
                assert proposal.state.value == "pending" and not docker.calls
                jobs = store.owner.store.layout.data_dir / "jobs"
                snapshot = jobs / str(proposal.manifest.inputs.snapshot_id)
                for path, mode in (
                    (jobs, 0o700),
                    (snapshot, 0o700),
                    (snapshot / "input", 0o555),
                    (snapshot / "input" / "nested", 0o555),
                    (snapshot / "input" / "source.py", 0o444),
                    (snapshot / "input" / "nested" / "part.py", 0o444),
                    (snapshot / "prepared.json", 0o600),
                    (jobs / ".prepare.lock", 0o600),
                ):
                    assert path.stat().st_mode & 0o777 == mode
                os.chmod(jobs, 0o600)
                try:
                    with pytest.raises(PreparedInputFault) as unsafe:
                        coordinator.propose(
                            project.root,
                            "python",
                            ("/usr/local/bin/python", "-V"),
                            ("source.py",),
                            request_id=uuid4(),
                            deadline=time.monotonic() + 5,
                        )
                    assert (
                        unsafe.value.code == "input_unsafe" and jobs.stat().st_mode & 0o777 == 0o600
                    )
                finally:
                    os.chmod(jobs, 0o700)
            finally:
                os.umask(old)
