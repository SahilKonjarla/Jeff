"""Cold configured startup activation and fresh owned execution-home policy."""

import os
import time
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest

from jeff.application.execution import DockerBackendFault, DockerCLIIdentity
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.runtime import ControlFault, ControlObservation, RuntimeIdentityData

from .support_composition import composed


def test_bootstrap_optin_publishes_capability_without_any_startup_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from jeff.application.execution import FreshExecutionCall
    from jeff.bootstrap.runtime import _serve
    from jeff.infrastructure.runtime_ipc import ControlClient, RuntimeProjectClient

    def no_process(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("startup must not run a child")

    monkeypatch.setattr("jeff.infrastructure.docker_cli.subprocess.Popen", no_process)

    def scenario_case(scenario: str) -> None:
        with composed(tmp_path / scenario, configured=scenario != "disabled") as (
            coordinator,
            store,
            project,
            docker,
        ):
            if scenario == "mismatch":
                configuration = store.execution_backend
                assert configuration is not None
                configuration.binary.path.chmod(0o700)
                configuration.binary.path.write_bytes(b"different-cli")
                configuration.binary.path.chmod(0o500)
            if scenario == "capacity":
                namespace = store.owner.store.layout.runtime_dir / "execution-backend"
                namespace.mkdir(mode=0o700)
                for _ in range(16):
                    (namespace / str(uuid4())).mkdir(mode=0o700)
            store.close(timeout=5)  # One SQL owner at a time, including in this source fixture.
            owner = store.owner
            token = owner.store.read_credential()
            identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
            reader, writer = os.pipe()

            def observe(stop: bool = False) -> ControlObservation:
                with ControlClient(owner.store.layout, token).connect(
                    time.monotonic() + 5
                ) as connection:
                    return connection.request("stop" if stop else "status", identity.instance_id)

            async def exercise() -> None:
                task = asyncio.create_task(_serve(owner, token, identity, writer))
                try:
                    hint = await asyncio.wait_for(asyncio.to_thread(os.read, reader, 512), 5)
                    assert hint  # Scheduling hint only; authenticated status below is authority.
                    status = await asyncio.to_thread(observe)
                    assert status.application_ready
                    assert ("execution.v1" in status.available_operations) == (
                        scenario == "enabled"
                    )
                    namespace = owner.store.layout.runtime_dir / "execution-backend"
                    assert namespace.exists() == (scenario != "disabled")
                    if scenario != "enabled":
                        call = FreshExecutionCall(
                            request_id=uuid4(),
                            directory=project.root,
                            operation="show",
                            approval_id=uuid4(),
                        )
                        reply = await asyncio.to_thread(
                            RuntimeProjectClient(owner.store.layout).fresh, call
                        )
                        assert reply.error is not None
                        expected = {
                            "mismatch": "execution_backend_unavailable",
                            "capacity": "execution_backend_capacity",
                            "disabled": "execution_unavailable",
                        }
                        assert reply.error.error.code == expected[scenario]
                finally:
                    if not task.done():
                        await asyncio.to_thread(observe, True)
                    await asyncio.wait_for(task, 5)

            old = os.umask(0o177)
            try:
                asyncio.run(exercise())
            finally:
                os.umask(old)
                os.close(reader)
            owner.check()
            owner.require_no_endpoint()
            assert not docker.calls and not docker.queries

    for scenario in ("enabled", "mismatch", "capacity", "disabled"):
        scenario_case(scenario)


def test_configured_backend_activation_pins_identity_without_startup_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jeff.application.projects import Failure
    from jeff.bootstrap.runtime import create_configured_execution

    def no_process(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("startup must not run any child")

    monkeypatch.setattr("jeff.infrastructure.docker_cli.subprocess.Popen", no_process)
    with composed(tmp_path / "ok", configured=True) as (coordinator, store, project, docker):
        assert store.execution_backend is not None
        result = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() + 5,
        )
        assert isinstance(result, ExecutionCoordinator) and not docker.calls and not docker.queries
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        assert set(p.name for p in root.iterdir()) == {str(coordinator.instance_id)}
        with pytest.raises(ControlFault):
            create_configured_execution(
                owner=store.owner,
                storage=store,
                projects=coordinator.projects,
                instance_id=uuid4(),
                deadline=time.monotonic() + 5,
            )
    with composed(tmp_path / "changed", configured=True) as (coordinator, store, project, docker):
        configuration = store.execution_backend
        assert configuration is not None
        configuration.binary.path.chmod(0o700)
        configuration.binary.path.write_bytes(b"different-cli")
        configuration.binary.path.chmod(0o500)
        refused = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() + 5,
        )
        assert (
            isinstance(refused, Failure) and refused.error.code == "execution_backend_unavailable"
        )
        assert store.snapshot().state == "ready" and not docker.calls and not docker.queries
    from jeff.infrastructure.docker_cli import DockerLifecycleCLI, DockerTargetInspector

    class WrongInspector(DockerTargetInspector):
        @property
        def identity(self) -> DockerCLIIdentity:
            return super().identity.model_copy(update={"sha256": "f" * 64})

    class WrongLifecycle(DockerLifecycleCLI):
        def __init__(
            self, binary: Path, config: Path, home: Path, *, deadline: float | None = None
        ) -> None:
            super().__init__(binary, config, home, deadline=deadline)
            self.identity = self.identity.model_copy(update={"size": self.identity.size + 1})

    for index, (name, replacement) in enumerate(
        (
            ("DockerTargetInspector", WrongInspector),
            ("DockerLifecycleCLI", WrongLifecycle),
        )
    ):
        with (
            composed(tmp_path / str(index), configured=True) as (
                coordinator,
                store,
                project,
                docker,
            ),
            monkeypatch.context() as patch,
        ):
            patch.setattr("jeff.bootstrap.runtime." + name, replacement)
            refused = create_configured_execution(
                owner=store.owner,
                storage=store,
                projects=coordinator.projects,
                instance_id=coordinator.instance_id,
                deadline=time.monotonic() + 5,
            )
            assert (
                isinstance(refused, Failure)
                and refused.error.code == "execution_backend_unavailable"
            )
    from jeff.application.storage import StorageFault

    with composed(tmp_path / "closing", configured=True) as (coordinator, store, project, docker):
        store.request_close()
        with pytest.raises(StorageFault):
            create_configured_execution(
                owner=store.owner,
                storage=store,
                projects=coordinator.projects,
                instance_id=coordinator.instance_id,
                deadline=time.monotonic() + 5,
            )
        assert not (store.owner.store.layout.runtime_dir / "execution-backend").exists()
    with composed(tmp_path / "expired", configured=True) as (coordinator, store, project, docker):
        refused = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() - 1,
        )
        assert (
            isinstance(refused, Failure)
            and not (store.owner.store.layout.runtime_dir / "execution-backend").exists()
        )
    with composed(tmp_path / "disabled") as (coordinator, store, project, docker):
        refused = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() + 5,
        )
        assert isinstance(refused, Failure) and refused.error.code == "execution_unavailable"
        assert not (store.owner.store.layout.runtime_dir / "execution-backend").exists()


def test_owned_execution_homes_are_fresh_private_bounded_and_never_repaired(tmp_path: Path) -> None:
    with composed(tmp_path / "valid") as (coordinator, store, project, docker):
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        old = os.umask(0o177)
        try:
            with store.owner.execution_homes(
                coordinator.instance_id, deadline=time.monotonic() + 5
            ) as homes:
                assert homes == (
                    root / str(coordinator.instance_id) / "metadata",
                    root / str(coordinator.instance_id) / "lifecycle",
                )
                for path in (root, homes[0].parent, *homes):
                    assert path.stat().st_mode & 0o777 == 0o700
                assert not tuple(homes[0].iterdir()) and not tuple(homes[1].iterdir())
                store.owner.check()
                store.index(project)  # Signed SQL recovery priming still validates the namespace.
                store.owner.check()
        finally:
            os.umask(old)
        with pytest.raises(DockerBackendFault):
            with store.owner.execution_homes(
                coordinator.instance_id, deadline=time.monotonic() + 5
            ):
                pass
        # Partial generations count; there is no recursive inspection or cleanup.
        for _ in range(15):
            (root / str(uuid4())).mkdir(mode=0o700)
        before = set(root.iterdir())
        with pytest.raises(DockerBackendFault) as full:
            with store.owner.execution_homes(uuid4(), deadline=time.monotonic() + 5):
                pass
        assert full.value.code == "docker_home_capacity" and set(root.iterdir()) == before
        assert not docker.calls and not docker.queries
    with composed(tmp_path / "unsafe") as (_, store, _, _):
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        root.mkdir(mode=0o700)
        root.chmod(0o600)
        try:
            with pytest.raises(DockerBackendFault):
                with store.owner.execution_homes(uuid4(), deadline=time.monotonic() + 5):
                    pass
            assert root.stat().st_mode & 0o777 == 0o600 and not tuple(root.iterdir())
        finally:
            root.chmod(0o700)
    with composed(tmp_path / "link") as (_, store, _, _):
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(DockerBackendFault):
            with store.owner.execution_homes(uuid4(), deadline=time.monotonic() + 5):
                pass
        assert root.is_symlink() and not tuple(outside.iterdir())
    with composed(tmp_path / "foreign") as (_, store, _, _):
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        root.mkdir(mode=0o700)
        (root / "unexpected").write_text("public fixture")
        with pytest.raises(DockerBackendFault):
            with store.owner.execution_homes(uuid4(), deadline=time.monotonic() + 5):
                pass
        assert {p.name for p in root.iterdir()} == {"unexpected"}
