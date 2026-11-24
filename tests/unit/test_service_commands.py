"""Service review and apply through the source CLI with a fake SQL/engine boundary."""

import json
import time
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from jeff.application.compose_lifecycle import ServiceAction, ServiceAttempt, ServiceResources
from jeff.application.compose_service_commands import ServiceCommands
from jeff.application.compose_service_worker import ServiceWorker
from jeff.application.dispatch import DispatchReply, ProjectCall, ProjectClient
from jeff.application.runtime import RuntimeIdentityData
from jeff.application.storage import GuardedRuntimeSettings, HistorySettings, StorageReadiness
from jeff.domain.projects import Project
from jeff.infrastructure.postgres_execution_actor import PostgresExecutionActor
from jeff.infrastructure.postgres_storage import PostgresStorage
from jeff.infrastructure.runtime_state import Ownership
from jeff.interfaces.cli import create_app

from test_compose_lifecycle import _resources, _start
from test_postgres_service_lifecycle import _Database


@pytest.mark.parametrize("uncertain_restart", [False, True])
def test_review_is_separate_from_apply_and_stop_comes_from_the_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uncertain_restart: bool,
) -> None:
    project = Project(uuid4(), tmp_path.resolve())
    monkeypatch.chdir(project.root)
    start = _start()
    start = start.model_copy(update={"target": start.target.model_copy(
        update={"project_id": project.project_id, "root": project.root},
    )})
    resources = _resources()
    db = _Database()
    owner = uuid4()
    state = RuntimeIdentityData(pid=1, instance_id=owner)
    effects: list[str] = []
    restart_fails = False

    def forbidden():
        raise AssertionError("No live process or database connection expected")

    storage = PostgresStorage(forbidden, RuntimeError, owner)
    monkeypatch.setattr(storage, "_transaction", db.transaction)
    journal = PostgresExecutionActor(
        cast(Ownership, object()),
        storage,
        GuardedRuntimeSettings(schema_version=4, history=HistorySettings(retention_days=30)),
        on_commit=lambda: None,
    )
    # Substitute only startup readiness: requests still use the actor's writer pool and errors.
    monkeypatch.setattr(
        journal,
        "_state",
        StorageReadiness(
            state="ready",
            backend="postgresql",
            schema_version=1,
            retention_days=30,
            settings_source="existing_file",
        ),
    )

    class Backend:
        def verify(self, action: ServiceAction, *, deadline: float) -> str:
            return action.digest

        def create(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
            effects.append("create")
            return resources

        def start(
            self, attempt: ServiceAttempt, created: ServiceResources, *, deadline: float
        ) -> ServiceResources:
            effects.append("start")
            return created

        def restart(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
            effects.append("restart")
            if restart_fails:
                raise RuntimeError("Restart result unconfirmed")
            return resources

        def stop(self, attempt: ServiceAttempt, *, deadline: float) -> ServiceResources:
            effects.append("stop")
            return resources

    backend = Backend()
    commands = ServiceCommands(journal, backend, ServiceWorker(journal, backend, owner), start, owner)

    class Client:
        def execute(self, call: ProjectCall) -> DispatchReply:
            assert call.operation == "service.lifecycle" and call.directory == project.root
            assert call.service_command is not None
            return DispatchReply(
                request_id=call.request_id, identity=state,
                result=commands.execute(project, call.request_id, call.service_command, time.monotonic() + 5),
            )

    app = create_app(lambda: cast(ProjectClient, Client()), forbidden)

    def cli(*args: str, code: int = 0):
        result = CliRunner().invoke(app, ["docker", "service", *args, "--json"])
        assert result.exit_code == code, (result.output, result.exception)
        return json.loads(result.stdout)

    try:
        db.schema_ready = False
        missing = cli("propose-start", "api", code=1)
        assert missing["error"]["code"] == "service_unavailable" and not db.authorities
        db.schema_ready = True
        reviewed = cli("propose-start", "api")["data"]
        aid, digest = reviewed["approval_id"], reviewed["digest"]
        assert digest == start.digest and reviewed["state"] == "pending"
        assert cli("apply", aid, "--digest", digest, code=1)["error"]["code"] == "service_conflict"
        cli("approve", aid, "--digest", "0" * 64, code=1)
        assert cli("approve", aid, "--digest", digest)["data"]["state"] == "approved"
        assert not effects
        assert cli("apply", aid, "--digest", digest)["data"]["outcome"] == "running"
        cli("apply", aid, "--digest", digest, code=1)
        restart = cli("propose-restart", reviewed["job_id"])["data"]
        assert restart["manifest"]["action"] == "restart" and restart["digest"] != digest
        assert restart["manifest"]["original"] == reviewed["manifest"]
        cli("approve", restart["approval_id"], "--digest", restart["digest"])
        assert effects == ["create", "start"]
        restart_fails = uncertain_restart
        response = cli(
            "apply", restart["approval_id"], "--digest", restart["digest"],
            code=1 if uncertain_restart else 0,
        )["data"]
        assert response["outcome"] == ("unknown" if uncertain_restart else "running")
        assert response["terminal_recorded"]
        cli("apply", restart["approval_id"], "--digest", restart["digest"], code=1)
        if uncertain_restart:
            assert response["health_relay"] is None and effects == ["create", "start", "restart"]
            return
        stop = cli("propose-stop", reviewed["job_id"])["data"]
        assert stop["manifest"]["resources"] == resources.model_dump(mode="json")
        assert stop["manifest"]["start_manifest_digest"] == digest
        assert effects == ["create", "start", "restart"]
        cli("approve", stop["approval_id"], "--digest", stop["digest"])
        assert cli("apply", stop["approval_id"], "--digest", stop["digest"])["data"]["outcome"] == "stopped"
        assert effects == ["create", "start", "restart", "stop"]
        cli("propose-restart", reviewed["job_id"], code=1)
    finally:
        journal.close()
