"""Restarted public CLI/RPC inspection is facts-only, with synthetic Docker I/O."""

import asyncio
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from jeff.application.dispatch import ProjectDispatcher
from jeff.application.execution import (
    DockerBackendFault,
    ExecutionCancelCall,
    FreshExecutionReply,
    ReviewFault,
)
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.questions import QuestionService
from jeff.application.runtime import RuntimeIdentityData
from jeff.domain.projects import Project
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_execution import DockerJobController
from jeff.infrastructure.runtime_ipc import ControlServer, RuntimeProjectClient
from jeff.infrastructure.runtime_output import GuardedOutputCollector
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitRepositoryRead
from jeff.interfaces.cli import create_app

from .support_composition import approved, composed
from .support_docker import Docker


@contextmanager
def restarted_coordinator(
    original: ExecutionCoordinator, previous: SQLiteStorage, docker: Docker
) -> Iterator[tuple[ExecutionCoordinator, SQLiteStorage]]:
    previous.close(timeout=5)
    generation = uuid4()
    store = SQLiteStorage(previous.owner, instance_id=generation)
    store.start()
    try:
        assert store.wait_ready(5).state == "ready"
        inputs = FilePreparedInputStore(store.owner)
        controller = DockerJobController(
            original.verifier, docker, inputs, store, owner_instance_id=generation
        )
        current = ExecutionCoordinator(
            original.projects,
            store,
            store,
            inputs,
            inputs,
            original.verifier,
            controller,
            store,
            GuardedOutputCollector,
            profiles=lambda: store.execution_profiles,
            declarations=original.declarations,
            instance_id=generation,
            proposal_lookup=store,
            inspection=store,
            stop_store=store,
        )
        yield current, store
    finally:
        store.close(timeout=5)


@pytest.mark.parametrize("state", ("completed", "revoked", "unknown"))
def test_restarted_cli_rpc_reads_history_without_backend_queries_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    with composed(tmp_path, cancellable=True) as (original, previous, project, docker):
        record = approved(original, project)
        if state == "completed":
            result = original.run(
                project.root,
                record.approval_id,
                record.manifest_digest,
                deadline=time.monotonic() + 5,
            )
            assert result.disposition == "completed"
        elif state == "revoked":
            stopped = original.submit_cancel(
                ExecutionCancelCall(
                    request_id=uuid4(),
                    approval_id=record.approval_id,
                    reviewed_manifest_digest=record.manifest_digest,
                ),
                deadline=time.monotonic() + 5,
            ).result(timeout=5)
            assert stopped.terminal is not None and stopped.terminal.evidence == "not_started"
        else:
            from jeff.application.execution import AuthorityOperation

            previous.authority(
                AuthorityOperation(
                    operation="claim",
                    project_id=project.project_id,
                    canonical_root=project.root,
                    owner_instance_id=original.instance_id,
                    approval_id=record.approval_id,
                    current_manifest_digest=record.manifest_digest,
                ),
                deadline=time.monotonic() + 5,
            )
        before = (list(docker.calls), list(docker.queries), len(docker.intents))
        with restarted_coordinator(original, previous, docker) as (current, store):
            assert current.instance_id != original.instance_id
            other = Project(uuid4(), tmp_path / "other")
            other.root.mkdir(mode=0o700)
            FileProjectStore().initialize(other)
            store.index(other)
            identity = RuntimeIdentityData(instance_id=current.instance_id, pid=os.getpid())
            workers = RuntimeDispatcher(
                ProjectDispatcher(
                    current.projects,
                    QuestionService(current.projects, GitRepositoryRead()),
                    store,
                    identity,
                ),
                fresh=current,
            )
            server = ControlServer(
                store.owner, store.owner.store.read_credential(), identity, store.snapshot
            )
            server.dispatcher = workers
            client = RuntimeProjectClient(store.owner.store.layout)
            monkeypatch.chdir(project.root)

            def forbidden() -> NoReturn:
                raise AssertionError("Historical inspection used an unrelated CLI factory")

            app = create_app(forbidden, forbidden, fresh_execution=lambda: client)
            runner = CliRunner()
            workers.start()

            async def exercise() -> None:
                task = asyncio.create_task(server.serve())
                try:
                    for _ in range(200):
                        if store.owner.store.layout.socket.exists():
                            break
                        if task.done():
                            await task
                        await asyncio.sleep(0.01)

                    def invoke(operation: str) -> FreshExecutionReply:
                        response = runner.invoke(
                            app, ["execution", operation, str(record.approval_id), "--json"]
                        )
                        assert response.exit_code == 0, response.output
                        return FreshExecutionReply.model_validate_json(response.stdout)

                    shown = await asyncio.to_thread(invoke, "show")
                    logs = await asyncio.to_thread(invoke, "logs")
                    assert shown.identity == logs.identity == current.instance_id
                    assert shown.view is not None and logs.view == shown.view
                    assert shown.view.manifest_digest == record.manifest_digest
                    assert shown.output is None and shown.admission is None
                    if state == "completed":
                        assert (
                            shown.completion_state == "recorded"
                            and shown.execution_phase == "finished"
                        )
                        assert logs.output is not None and logs.output.stdout == "public output\n"
                        assert (
                            shown.completion is not None and shown.completion.outcome.exit_code == 0
                        )
                    elif state == "revoked":
                        assert (
                            shown.completion_state == "stop_recorded"
                            and shown.execution_phase == "finished"
                        )
                        assert shown.stop is not None and shown.stop.evidence == "not_started"
                        assert shown.stop.cleanup_verified and logs.output is None
                    else:
                        assert (
                            shown.completion_state == "claim_only"
                            and shown.execution_phase == "unconfirmed"
                        )
                        assert shown.view.claim_state == "unknown"
                        assert (
                            shown.completion is None and shown.stop is None and logs.output is None
                        )
                    for reply in (shown, logs):
                        payload = reply.model_dump_json()
                        assert all(
                            '"' + key + '"' not in payload
                            for key in (
                                "owner_instance_id",
                                "claim_id",
                                "launch_claim",
                                "owned",
                                "nonce",
                                "container_id",
                            )
                        )
                    # Another registered project cannot use a guessed historical approval UUID.
                    from jeff.application.execution import FreshExecutionCall

                    refused = await asyncio.to_thread(
                        client.fresh,
                        FreshExecutionCall(
                            request_id=uuid4(),
                            operation="logs",
                            directory=other.root,
                            approval_id=record.approval_id,
                        ),
                    )
                    assert not refused.ok and refused.view is None and refused.output is None
                finally:
                    server.request_stop()
                    await asyncio.wait_for(task, 5)

            mask = os.umask(0o177)
            try:
                asyncio.run(exercise())
            finally:
                try:
                    workers.close(timeout=5)
                finally:
                    os.umask(mask)
            for action in ("decide", "start", "run"):
                with pytest.raises(DockerBackendFault, match="docker_authority_required"):
                    if action == "decide":
                        current.decide(
                            project.root,
                            record.approval_id,
                            record.manifest_digest,
                            approve=True,
                            deadline=time.monotonic() + 5,
                        )
                    elif action == "start":
                        current.start(
                            project.root,
                            record.approval_id,
                            record.manifest_digest,
                            request_id=uuid4(),
                            admission_deadline=time.monotonic() + 5,
                            on_admitted=lambda _: None,
                        )
                    else:
                        current.run(
                            project.root,
                            record.approval_id,
                            record.manifest_digest,
                            deadline=time.monotonic() + 5,
                        )
            with pytest.raises(ReviewFault, match="execution_target_mismatch"):
                current.submit_cancel(
                    ExecutionCancelCall(
                        request_id=uuid4(),
                        approval_id=record.approval_id,
                        reviewed_manifest_digest=record.manifest_digest,
                    ),
                    deadline=time.monotonic() + 5,
                ).result(timeout=5)
            assert (docker.calls, docker.queries, len(docker.intents)) == before
            assert workers.actions_drained
