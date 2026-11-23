"""Installed-shape CLI and authenticated project RPC with a fake Docker adapter."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Never
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from jeff.application.compose_status import ComposeStatusTarget
from jeff.application.dispatch import ProjectCall, ProjectDispatcher
from jeff.application.execution import DockerBackendFault, DockerCLIIdentity
from jeff.application.questions import QuestionService
from jeff.application.runtime import RuntimeIdentityData
from jeff.application.storage import GuardedRuntimeSettings
from jeff.bootstrap.runtime import compose_status as composition
from jeff.infrastructure.docker_cli.compose import ComposeStatus
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_ipc import ControlServer, RuntimeProjectClient
from jeff.infrastructure.runtime_settings import parse_settings
from jeff.integrations.git import GitRepositoryRead
from jeff.interfaces.cli import create_app

from .support_composition import composed


@pytest.mark.parametrize("mode", ["found", "empty", "ambiguous", "unavailable", "foreign"])
def test_status_cli_via_authenticated_project_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        store.index(project, deadline=time.monotonic() + 5)
        binary = DockerCLIIdentity(path=tmp_path / "docker", sha256="a" * 64, size=1)
        endpoint = "unix:///synthetic/docker.sock"
        target = ComposeStatusTarget(
            project_id=uuid4() if mode == "foreign" else project.project_id,
            root=project.root,
            endpoint=endpoint,
            project="sandbox-project",
            service="api",
            binary=binary,
            config=tmp_path / "config",
            home=tmp_path / "home",
        )
        settings = parse_settings(
            json.dumps(
                {
                    "schema_version": 4,
                    "history": {"retention_days": 30},
                    "compose_status": target.model_dump(mode="json"),
                }
            ).encode()
        )
        assert isinstance(settings, GuardedRuntimeSettings) and settings.backend is None
        assert settings.compose_status == target
        calls: list[str] = []

        class FakeCLI:
            identity = binary

            def __init__(self, *args: object, **kwargs: object) -> None:
                assert mode != "foreign"

            def compose_status(self, **kwargs: object) -> ComposeStatus:
                assert kwargs["endpoint"] == endpoint and kwargs["root"] == project.root
                assert (kwargs["project"], kwargs["service"]) == ("sandbox-project", "api")
                calls.append("observe")
                if mode == "unavailable":
                    raise DockerBackendFault("docker_unavailable")
                return ComposeStatus(("running",) if mode == "found" else (), mode == "ambiguous")

        monkeypatch.setattr(composition, "ComposeInspectionCLI", FakeCLI)
        identity = RuntimeIdentityData(instance_id=coordinator.instance_id, pid=os.getpid())
        workers = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
                compose_status=composition.compose_status_reader(target),
            )
        )
        layout = store.owner.store.layout
        server = ControlServer(store.owner, store.owner.store.read_credential(), identity, store.snapshot)
        server.dispatcher = workers
        client = RuntimeProjectClient(layout)
        monkeypatch.chdir(project.root)

        def forbidden() -> Never:
            raise AssertionError("Unrelated CLI factory invoked")

        def invoke() -> None:
            app = create_app(lambda: client, forbidden)
            result = CliRunner().invoke(app, ["docker", "status", "--json"])
            payload = json.loads(result.stdout)
            if mode in ("foreign", "unavailable"):
                expected = (
                    "compose_status_not_configured" if mode == "foreign" else "docker_unavailable"
                )
                assert result.exit_code == 1 and payload["error"]["code"] == expected
            else:
                assert result.exit_code == 0 and payload["data"] == {
                    "compose_project": "sandbox-project",
                    "service": "api",
                    "states": ["running"] if mode == "found" else [],
                    "ambiguous": mode == "ambiguous",
                }
            reply = client.execute(ProjectCall(operation="docker.status", directory=project.root))
            assert reply.identity == identity and reply.receipt is None and reply.accepted is False

        async def exercise() -> None:
            serving = asyncio.create_task(server.serve())
            try:
                for _ in range(200):
                    if layout.socket.exists():
                        break
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
                await asyncio.to_thread(invoke)
            finally:
                server.request_stop()
                await asyncio.wait_for(serving, 5)

        previous_umask = os.umask(0o177)
        workers.start()
        try:
            asyncio.run(exercise())
            assert calls == ([] if mode == "foreign" else ["observe", "observe"])
            assert not docker.calls
        finally:
            try:
                workers.close(timeout=5)
            finally:
                os.umask(previous_umask)
