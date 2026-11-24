"""Installed-shape CLI and authenticated project RPC with a fake Docker adapter."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Never
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from jeff.application.compose_service_commands import (
    ServiceCommand,
    ServiceCommands,
    ServiceCommandSuccess,
)
from jeff.application.compose_service_worker import ServiceWorkResult
from jeff.application.compose_status import ComposeLogsData, ComposeStatusTarget, PublishedPort
from jeff.application.dispatch import ProjectCall, ProjectDispatcher
from jeff.application.execution import DockerBackendFault, DockerCLIIdentity
from jeff.application.process_ports import (
    NativeListener,
    ProcessListData,
    ProcessListSuccess,
    ProcessPortsData,
    ProcessPortsSuccess,
)
from jeff.application.questions import QuestionService
from jeff.application.runtime import RuntimeIdentityData
from jeff.application.storage import GuardedRuntimeSettings
from jeff.bootstrap.runtime import compose_status as composition
from jeff.domain.projects import Project
from jeff.infrastructure.docker_cli.compose import ComposeStatus
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_ipc import ControlServer, RuntimeProjectClient
from jeff.infrastructure.runtime_settings import parse_settings
from jeff.integrations.git import GitRepositoryRead
from jeff.interfaces.cli import create_app

from .support_composition import composed


@pytest.mark.parametrize(
    "mode",
    [
        "found", "empty", "ambiguous", "unavailable", "foreign", "ports", "process-list",
        "logs", "logs-disabled", "logs-other-service", "service-apply",
    ],
)
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
            allow_logs=mode in ("logs", "logs-other-service"),
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
                assert mode not in ("foreign", "logs-disabled", "logs-other-service")

            def compose_status(self, **kwargs: object) -> ComposeStatus:
                assert kwargs["endpoint"] == endpoint and kwargs["root"] == project.root
                assert (kwargs["project"], kwargs["service"]) == ("sandbox-project", "api")
                calls.append("observe")
                if mode == "unavailable":
                    raise DockerBackendFault("docker_unavailable")
                if mode == "found":
                    port = PublishedPort(
                        host_ip="127.0.0.1", host_port=53129, container_port=8000, protocol="tcp"
                    )
                    return ComposeStatus(("running",), False, ("unhealthy",), ((port,),))
                return ComposeStatus((), mode == "ambiguous")

            def compose_logs(self, **kwargs: object) -> ComposeLogsData:
                assert kwargs["endpoint"] == endpoint and kwargs["root"] == project.root
                assert (kwargs["project"], kwargs["service"]) == ("sandbox-project", "api")
                calls.append("logs")
                return ComposeLogsData(compose_project="sandbox-project", service="api", stdout="ready\n")

        monkeypatch.setattr(composition, "ComposeInspectionCLI", FakeCLI)

        def ports(target: Project, deadline: float) -> ProcessPortsSuccess:
            assert mode == "ports" and target == project and deadline > time.monotonic()
            return ProcessPortsSuccess(
                data=ProcessPortsData(listeners=(NativeListener(pid=42, port=8000),))
            )

        def processes(target: Project, deadline: float) -> ProcessListSuccess:
            assert mode == "process-list" and target == project and deadline > time.monotonic()
            return ProcessListSuccess(data=ProcessListData(pids=(42,)))

        service_commands = object.__new__(ServiceCommands) if mode == "service-apply" else None
        approval = uuid4()

        def apply(
            target: Project, request_id: UUID, command: ServiceCommand, deadline: float
        ) -> ServiceCommandSuccess:
            assert target == project and deadline > time.monotonic()
            assert command == ServiceCommand(operation="apply", approval_id=approval, digest="b" * 64)
            return ServiceCommandSuccess(data=ServiceWorkResult(
                request_id=request_id, outcome="running", terminal_recorded=True,
            ))

        if service_commands is not None:
            monkeypatch.setattr(service_commands, "execute", apply)
        identity = RuntimeIdentityData(instance_id=coordinator.instance_id, pid=os.getpid())
        workers = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
                compose_status=composition.compose_status_reader(target),
                compose_logs=composition.compose_logs_reader(target),
                process_ports=ports,
                process_list=processes,
                service_commands=service_commands,
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
            if mode == "service-apply":
                result = CliRunner().invoke(app, [
                    "docker", "service", "apply", str(approval), "--digest", "b" * 64, "--json",
                ])
                assert result.exit_code == 0, result.output
                assert json.loads(result.stdout)["data"]["outcome"] == "running"
                return
            if mode.startswith("logs"):
                service = "other" if mode == "logs-other-service" else "api"
                result = CliRunner().invoke(app, ["docker", "logs", service, "--json"])
                payload = json.loads(result.stdout)
                if mode == "logs":
                    assert result.exit_code == 0 and payload["data"]["stdout"] == "ready\n"
                else:
                    assert result.exit_code == 1
                    assert payload["error"]["code"] == "compose_logs_not_configured"
                return
            if mode == "process-list":
                result = CliRunner().invoke(app, ["process", "list", "--json"])
                assert result.exit_code == 0, result.output
                assert json.loads(result.stdout)["data"]["pids"] == [42]
                return
            if mode == "ports":
                result = CliRunner().invoke(app, ["process", "ports", "--json"])
                assert result.exit_code == 0, result.output
                assert json.loads(result.stdout)["data"]["listeners"] == [
                    {"pid": 42, "port": 8000, "address": "127.0.0.1"}
                ]
                return
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
                    "health": ["unhealthy"] if mode == "found" else [],
                    "published_ports": [[
                        {
                            "host_ip": "127.0.0.1", "host_port": 53129,
                            "container_port": 8000, "protocol": "tcp",
                        }
                    ]] if mode == "found" else [],
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
            if mode == "logs":
                assert calls == ["logs"]
            elif mode in ("found", "empty", "ambiguous", "unavailable"):
                assert calls == ["observe", "observe"]
            else:
                assert not calls
            assert not docker.calls
        finally:
            try:
                workers.close(timeout=5)
            finally:
                os.umask(previous_umask)
