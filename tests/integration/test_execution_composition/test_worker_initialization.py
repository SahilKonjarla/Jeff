"""Existing worker initialization/drain and cold factory admission without hidden queues."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest

from jeff.application.dispatch import DispatchReply, ProjectCall, ProjectDispatcher
from jeff.application.execution import DockerBackendFault
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.questions import QuestionService
from jeff.application.runtime import ControlFault, RuntimeIdentityData
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.integrations.git import GitRepositoryRead

from .support_composition import approved, composed, pending


def test_backend_initializer_uses_existing_worker_and_gates_readiness_and_drain(
    tmp_path: Path,
) -> None:
    from jeff.application.execution import fresh_execution_failure
    from jeff.application.projects import Failure
    from jeff.application.storage import StorageFault

    def scenario_case(scenario: str) -> None:
        with composed(tmp_path / scenario) as (coordinator, store, project, docker):
            assert store.instance_id is not None
            identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
            entered = threading.Event()
            release = threading.Event()
            names: list[str] = []

            def initialize() -> ExecutionCoordinator | Failure:
                names.append(threading.current_thread().name)
                entered.set()
                if scenario == "held":
                    assert release.wait(3)
                if scenario == "fatal":
                    raise StorageFault("storage_failed")
                return (
                    fresh_execution_failure("execution_backend_unavailable")
                    if scenario == "unavailable"
                    else coordinator
                )

            workers = RuntimeDispatcher(
                ProjectDispatcher(
                    coordinator.projects,
                    QuestionService(coordinator.projects, GitRepositoryRead()),
                    store,
                    identity,
                ),
                fresh_factory=initialize,
            )
            workers.start()
            try:
                assert entered.wait(2) and names == ["jeff-project-0"]
                if scenario == "held":
                    assert not workers.ready and not workers.actions_drained
                    assert not workers.initialization.cancel()
                    assert (
                        len(
                            [t for t in threading.enumerate() if t.name.startswith("jeff-project-")]
                        )
                        == 4
                    )
                    with pytest.raises(ControlFault):
                        workers.submit(
                            ProjectCall(directory=project.root, operation="config"),
                            time.monotonic() + 1,
                        )
                    with pytest.raises(ControlFault) as closing:
                        workers.close(timeout=0)
                    assert (
                        closing.value.code == "runtime_stop_unconfirmed"
                        and not workers.actions_drained
                    )
                    release.set()
                    workers.close(timeout=5)
                    assert workers.fresh is None
                elif scenario == "fatal":
                    with pytest.raises(StorageFault):
                        workers.initialization.result(timeout=2)
                    assert not workers.ready and workers.fresh is None
                else:
                    workers.initialization.result(timeout=2)
                    assert workers.ready
                    if scenario == "success":
                        assert workers.fresh is coordinator and workers.fresh_issue is None
                    else:
                        assert workers.fresh is None and workers.fresh_issue is not None
                    configured = workers.submit(
                        ProjectCall(directory=project.root, operation="config"),
                        time.monotonic() + 5,
                    ).result(timeout=2)
                    assert isinstance(configured, DispatchReply) and configured.result.ok
            finally:
                release.set()
                workers.close(timeout=5)
            assert (
                workers.actions_drained
                and workers.closed
                and not docker.calls
                and not docker.queries
            )

    for scenario in ("held", "success", "unavailable", "fatal"):
        scenario_case(scenario)


def test_internal_factory_is_cold_and_composition_rejects_busy_without_queueing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jeff.bootstrap.runtime import create_internal_execution

    with composed(tmp_path) as (coordinator, store, project, docker):
        binary = tmp_path / "docker"
        binary.write_bytes(b"synthetic-cli")
        binary.chmod(0o500)
        config = tmp_path / "cfg"
        config.mkdir(mode=0o700)
        settings = config / "config.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        meta = tmp_path / "m"
        life = tmp_path / "l"
        meta.mkdir(mode=0o700)
        life.mkdir(mode=0o700)

        def no_process(*args: object, **kwargs: object) -> NoReturn:
            raise AssertionError("Cold factory must not execute any child.")

        monkeypatch.setattr("jeff.infrastructure.docker_cli.subprocess.Popen", no_process)
        built = create_internal_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            binary=binary,
            config=config,
            metadata_home=meta,
            lifecycle_home=life,
            declarations=coordinator.declarations,
        )
        assert isinstance(built, ExecutionCoordinator) and not docker.calls and not docker.queries
        with pytest.raises(DockerBackendFault):
            create_internal_execution(
                owner=store.owner,
                storage=store,
                projects=coordinator.projects,
                instance_id=uuid4(),
                binary=binary,
                config=config,
                metadata_home=meta,
                lifecycle_home=life,
                declarations=coordinator.declarations,
            )
        record = approved(coordinator, project)
        entered = threading.Event()
        release = threading.Event()
        docker.entered = entered
        docker.release = release
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(
                coordinator.run,
                project.root,
                record.approval_id,
                record.manifest_digest,
                deadline=time.monotonic() + 5,
            )
            try:
                assert entered.wait(2)
                with pytest.raises(DockerBackendFault) as busy:
                    pending(coordinator, project)
                assert busy.value.code == "docker_busy"
            finally:
                release.set()
            assert future.result(timeout=5).disposition == "completed"
        assert docker.calls == ["create", "attach", "remove"]
