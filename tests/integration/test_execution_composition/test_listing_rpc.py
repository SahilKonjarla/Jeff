"""Source CLI to authenticated local RPC listing; fake Docker, no native daemon."""

import asyncio
import os
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Never
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from jeff.application.dispatch import FreshExecutionRequest, ProjectDispatcher
from jeff.application.execution import (
    FreshExecutionCall,
    FreshExecutionPage,
    FreshExecutionReply,
    FreshExecutionView,
)
from jeff.application.questions import QuestionService
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, RuntimeIdentityData
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_ipc import (
    ControlClient,
    ControlServer,
    RuntimeProjectClient,
    encode_message,
    receive_message,
)
from jeff.integrations.git import GitRepositoryRead
from jeff.interfaces.cli import create_app

from .support_composition import composed, pending


@pytest.mark.parametrize("repeat", [False, True])
def test_client_rejects_over_limit_or_repeated_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repeat: bool,
) -> None:
    # Injected transport result, not authenticated RPC evidence; real RPC is below.
    with composed(tmp_path) as (coordinator, store, project, docker):
        views = tuple(
            sorted(
                (FreshExecutionView.from_record(pending(coordinator, project)) for _ in range(2)),
                key=lambda item: (item.created_at_ms, str(item.proposal_id)),
                reverse=True,
            )
        )
        after = f"{views[-1].created_at_ms}:{views[-1].proposal_id}" if repeat else None
        call = FreshExecutionCall(
            request_id=uuid4(),
            directory=project.root,
            operation="jobs.list",
            limit=2 if repeat else 1,
            after=after,
        )
        identity = RuntimeIdentityData(instance_id=coordinator.instance_id, pid=os.getpid())
        response = FreshExecutionReply(
            ok=True,
            request_id=call.request_id,
            operation=call.operation,
            identity=identity.instance_id,
            page=FreshExecutionPage(
                project_id=project.project_id, canonical_root=project.root, items=views
            ),
        )
        client = RuntimeProjectClient(store.owner.store.layout)
        monkeypatch.setattr(
            client, "_connect", lambda _: nullcontext(SimpleNamespace(identity=identity))
        )
        monkeypatch.setattr(client, "_exchange", lambda *args: response)
        reply = client.fresh(call)
        assert not reply.ok and reply.page is None and reply.error is not None
        assert reply.error.error.code == "request_outcome_unknown" and not docker.calls


def test_cli_authenticated_rpc_lists_only_current_project_without_dispatching_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        records = [pending(coordinator, project) for _ in range(3)]
        identity = RuntimeIdentityData(instance_id=coordinator.instance_id, pid=os.getpid())
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

        def forbidden() -> Never:
            raise AssertionError("Unrelated CLI factory invoked")

        runner = CliRunner()
        app = create_app(forbidden, forbidden, fresh_execution=lambda: client)

        def invoke(kind: str, after: str | None = None) -> FreshExecutionReply:
            args = ["execution", kind, "list", "--limit", "2", "--json"]
            if after is not None:
                args.extend(["--after", after])
            result = runner.invoke(app, args)
            assert result.exit_code == 0, result.output
            return FreshExecutionReply.model_validate_json(result.stdout)

        def stale_identity() -> FreshExecutionReply:
            layout = store.owner.store.layout
            with ControlClient(layout, store.owner.store.read_credential()).connect(
                time.monotonic() + 5
            ) as connection:
                request = FreshExecutionRequest(
                    expected_instance_id=uuid4(),
                    call=FreshExecutionCall(
                        request_id=uuid4(), directory=project.root, operation="jobs.list"
                    ),
                    remaining_ms=5000,
                )
                connection.connection.sendall(
                    encode_message(
                        request.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes
                    )
                )
                return receive_message(
                    connection.connection,
                    DEFAULT_RUNTIME_LIMITS.response_bytes,
                    time.monotonic() + 5,
                    FreshExecutionReply,
                )

        async def exercise() -> None:
            task = asyncio.create_task(server.serve())
            try:
                for _ in range(200):
                    if store.owner.store.layout.socket.exists():
                        break
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
                for kind in ("approvals", "jobs"):
                    first = await asyncio.to_thread(invoke, kind)
                    assert first.ok and first.identity == coordinator.instance_id
                    assert first.page is not None and first.page.next_after is not None
                    last = await asyncio.to_thread(invoke, kind, first.page.next_after)
                    assert last.page is not None and last.page.next_after is None
                    items = first.page.items + last.page.items
                    assert len(items) == 3
                    assert {item.approval_id for item in items} == {r.approval_id for r in records}
                refused = await asyncio.to_thread(stale_identity)
                assert not refused.ok and refused.page is None and refused.error is not None
                assert refused.error.error.code == "project_target_mismatch"
                assert not docker.calls and workers.actions_drained
            finally:
                server.request_stop()
                await asyncio.wait_for(task, 5)

        previous_umask = os.umask(0o177)
        workers.start()
        try:
            asyncio.run(exercise())
        finally:
            try:
                workers.close(timeout=5)
            finally:
                os.umask(previous_umask)
