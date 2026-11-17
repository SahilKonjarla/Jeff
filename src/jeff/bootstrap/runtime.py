"""Internal control child composition; not a public CLI lifecycle command."""

import argparse
import asyncio
import fcntl
import os
import stat
import sys
import time
from pathlib import Path
from typing import cast
from uuid import UUID

from jeff.application.diagnostics import RuntimeDiagnostics
from jeff.application.dispatch import (
    ActivityClient,
    ProjectClient,
    ProjectDispatcher,
    ProjectStatusService,
)
from jeff.application.execution import (
    DockerBackendFault,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    ExecutionCancelClient,
    ExecutionClient,
    ExecutionService,
    FreshExecutionClient,
    fresh_execution_failure,
)
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.projects import Failure, ProjectService
from jeff.application.questions import QuestionService
from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    PROTOCOL_REVISION,
    ControlFault,
    ControlIssueCode,
    RuntimeControl,
    RuntimeIdentityData,
    RuntimeLayout,
    RuntimePathInputs,
    StartupResult,
)
from jeff.application.storage import StorageFault
from jeff.infrastructure.docker_cli import DockerLifecycleCLI, DockerTargetInspector
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_events import RuntimeEvents
from jeff.infrastructure.runtime_execution import DockerJobController
from jeff.infrastructure.runtime_ipc import (
    ControlServer,
    RuntimeProjectClient,
    _stream_send,
    encode_message,
)
from jeff.infrastructure.runtime_output import GuardedOutputCollector
from jeff.infrastructure.runtime_paths import resolve_layout
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead
from jeff.integrations.git.worker import OwnedGitRunner


def create_internal_execution(
    *,
    owner: Ownership,
    storage: SQLiteStorage,
    projects: ProjectService,
    instance_id: UUID,
    binary: Path,
    config: Path,
    metadata_home: Path,
    lifecycle_home: Path,
    declarations: tuple[DockerTargetDeclaration, ...],
    expected_binary: DockerCLIIdentity | None = None,
    deadline: float | None = None,
) -> ExecutionCoordinator:
    """Construct adapters without Docker queries, implicit discovery or new workers.

    Trusted startup or an internal caller supplies owned SQL and private fresh HOME paths.
    Construction validates those explicit resources but never queries/launches Docker,
    creates directories, loads default user state or starts another worker.
    """
    owner.check()
    if (
        storage.owner is not owner
        or storage.instance_id != instance_id
        or storage.snapshot().state != "ready"
    ):
        raise DockerBackendFault("docker_authority_required")
    if (
        metadata_home == lifecycle_home
        or metadata_home in lifecycle_home.parents
        or lifecycle_home in metadata_home.parents
    ):
        raise DockerBackendFault("docker_unsafe_configuration")
    inputs = FilePreparedInputStore(owner)
    verifier = DockerTargetInspector(binary, config, home=metadata_home, deadline=deadline)
    backend = DockerLifecycleCLI(binary, config, lifecycle_home, deadline=deadline)
    if expected_binary is not None and (
        verifier.identity != expected_binary or backend.identity != expected_binary
    ):
        raise DockerBackendFault("docker_unsafe_configuration")
    controller = DockerJobController(
        verifier, backend, inputs, storage, owner_instance_id=instance_id
    )
    return ExecutionCoordinator(
        projects,
        storage,
        storage,
        inputs,
        inputs,
        verifier,
        controller,
        storage,
        GuardedOutputCollector,
        profiles=lambda: storage.execution_profiles,
        declarations=declarations,
        instance_id=instance_id,
        proposal_lookup=storage,
        inspection=storage,
        stop_store=storage,
    )


def create_configured_execution(
    *,
    owner: Ownership,
    storage: SQLiteStorage,
    projects: ProjectService,
    instance_id: UUID,
    deadline: float,
) -> ExecutionCoordinator | Failure:
    """Owned startup construction; never query Docker, discover defaults or repair state."""

    def active() -> None:
        owner.check()
        if storage.owner is not owner or storage.instance_id != instance_id:
            raise ControlFault("runtime_ownership_unconfirmed")
        if storage.snapshot().state != "ready":
            raise StorageFault("storage_closing")

    active()
    configuration = storage.execution_backend
    if configuration is None:
        return fresh_execution_failure("execution_unavailable")
    try:
        with owner.execution_homes(instance_id, deadline=deadline) as (metadata, lifecycle):
            built = create_internal_execution(
                owner=owner,
                storage=storage,
                projects=projects,
                instance_id=instance_id,
                binary=configuration.binary.path,
                config=configuration.config,
                metadata_home=metadata,
                lifecycle_home=lifecycle,
                declarations=configuration.declarations,
                expected_binary=configuration.binary,
                deadline=deadline,
            )
        active()
        return built
    except DockerBackendFault as error:
        active()  # Never disguise lost owner/storage readiness as optional backend failure.
        return fresh_execution_failure(
            "execution_backend_capacity"
            if error.code == "docker_home_capacity"
            else "execution_backend_unavailable"
        )


def _selected_layout() -> RuntimeLayout:
    """Select needed host inputs lazily, without probing or preparing installation state."""
    if sys.platform != "darwin":
        raise ControlFault("unsupported_runtime_platform")
    data = os.environ.get("JEFF_DATA_DIR")
    runtime = os.environ.get("JEFF_RUNTIME_DIR")
    # tempfile.gettempdir() probes writability by creating files. Selection/status must
    # not do that. Use standard Darwin TMPDIR metadata (or the canonical OS /tmp base),
    # and do not even resolve unused host locations when real overrides are supplied.
    layout = resolve_layout(
        RuntimePathInputs(
            platform=sys.platform,
            home=Path.home() if data is None else Path("/"),
            temp_dir=Path(os.environ.get("TMPDIR", "/private/tmp"))
            if runtime is None
            else Path("/"),
            uid=os.geteuid(),
            data_dir=data,
            runtime_dir=runtime,
        )
    )
    if isinstance(layout, Failure):
        raise ControlFault(cast(ControlIssueCode, layout.error.code))
    return layout


def create_runtime_diagnostics() -> RuntimeDiagnostics:
    """Lazy doctor composition; selection/inspection creates no installation state."""
    from jeff.infrastructure.runtime_diagnostics import RuntimeDiagnosticsAdapter

    return RuntimeDiagnosticsAdapter(_selected_layout)


def create_runtime_controller() -> RuntimeControl:
    """Select only explicit overrides/needed host metadata; no filesystem probes."""
    return RuntimeController(_selected_layout())


def create_project_client() -> ProjectClient:
    """Lazy location selection; local Git is composed only for read-only status."""
    return RuntimeProjectClient(
        _selected_layout,
        diagnostics=ProjectStatusService(
            ProjectService(GitProjectDiscovery(), FileProjectStore()), create_runtime_controller
        ),
    )


def create_execution_client() -> ExecutionClient:
    """Review-only RPC client; no installation or backend access at construction."""
    return RuntimeProjectClient(_selected_layout)


def create_cancellation_client() -> ExecutionCancelClient:
    """Lazy authenticated cancellation client; no cwd, defaults discovery or job adoption."""
    return RuntimeProjectClient(_selected_layout)


def create_fresh_execution_client() -> FreshExecutionClient:
    """Fresh RPC client only; constructing it never enables a daemon backend."""
    return RuntimeProjectClient(_selected_layout)


def create_activity_client() -> ActivityClient:
    """Lazy installed subscription interface; no CLI/auto-start/cursor persistence."""
    return RuntimeProjectClient(_selected_layout)


async def _serve(
    owner: Ownership, token: bytes, identity: RuntimeIdentityData, result_fd: int
) -> None:
    """Compose one owner and drain actual worker/helper/SQL lifetimes before exit.

    The startup pipe is diagnostic only; authenticated full readiness remains the
    parent's authority. Shutdown waits for resources rather than cancelling waits.
    """
    activation_deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.start_seconds
    events = RuntimeEvents(
        identity,
        _stream_send,
        lambda: storage.snapshot().state == "ready" and storage.notification_healthy,
        lambda: storage.retention_position(),
    )
    # D3 invalidates buffered pages using checked committed positions before loop hints.
    storage = SQLiteStorage(
        owner,
        instance_id=identity.instance_id,
        commit_hint=events.signal,
        scheduled_retention=True,
    )
    dispatcher: RuntimeDispatcher | None = None

    def duplicate_git_lease() -> int:
        """Share the existing flock description; a newly opened lock would not do so."""
        owner.check()
        return os.dup(owner.fd)

    git_runner = OwnedGitRunner(duplicate_git_lease)
    server = ControlServer(
        owner, token, identity, storage.snapshot, on_stop=storage.pause_retention
    )
    server.events = events
    serving = asyncio.create_task(server.serve())
    try:
        storage.start()
        while storage.snapshot().state == "initializing":
            if serving.done():
                await serving
            await asyncio.sleep(0.01)
        if serving.done():
            await serving  # publication/listener failure cannot become application readiness
        state = storage.snapshot()
        if state.state == "ready" and not server.stopping:
            projects = ProjectService(
                GitProjectDiscovery(process_runner=git_runner), FileProjectStore()
            )
            dispatcher = RuntimeDispatcher(
                ProjectDispatcher(
                    projects,
                    QuestionService(projects, GitRepositoryRead(process_runner=git_runner)),
                    storage,
                    identity,
                ),
                ExecutionService(
                    projects, storage, lambda: storage.execution_profiles, identity.instance_id
                ),
                fresh_factory=(
                    lambda: create_configured_execution(
                        owner=owner,
                        storage=storage,
                        projects=projects,
                        instance_id=identity.instance_id,
                        deadline=activation_deadline,
                    )
                )
                if storage.execution_backend is not None
                else None,
            )
            dispatcher.start()
            server.dispatcher = dispatcher
            try:
                await asyncio.shield(asyncio.wrap_future(dispatcher.initialization))
            except (ControlFault, StorageFault):
                raise
            except Exception:
                raise ControlFault("runtime_start_failed") from None
            if not server.stopping:
                events.start(dispatcher)
        packet = encode_message(
            StartupResult(
                revision=PROTOCOL_REVISION, instance_id=identity.instance_id, error=state.error
            ).model_dump(mode="json"),
            512,
        )
        try:
            if os.write(result_fd, packet) != len(packet):
                raise ControlFault("runtime_start_failed")
        except BrokenPipeError:
            pass  # authenticated readiness can precede this diagnostic-only result
        finally:
            os.close(result_fd)
            result_fd = -1
        if state.state != "ready":
            server.request_stop()
        await serving
    finally:
        if result_fd >= 0:
            os.close(result_fd)
        server.request_stop()
        try:
            await serving
        finally:
            # No successful shutdown after the original absolute budget. If a
            # cooperative syscall/thread outlives it, main still retains the raw
            # inherited ownership FD until kernel exit; no PID signalling/adoption.
            try:
                if dispatcher is not None:
                    dispatcher.request_close()
                    while not dispatcher.closed:
                        server.check_stop_budget()
                        await asyncio.sleep(0.01)
                    dispatcher.close(timeout=0)
                while not git_runner.closed:
                    server.check_stop_budget()
                    await asyncio.sleep(0.01)
            finally:
                # Seal SQL even on drain failure, without claiming a live actor has exited.
                storage.request_close()
                while not storage.closed:
                    server.check_stop_budget()
                    await asyncio.sleep(0.01)
                storage.close(timeout=0)


def main() -> int:
    """Validate inherited FDs, inspect metadata, then adopt before reading the key."""
    parser = argparse.ArgumentParser(
        description="Internal owned daemon child; use public jeff start."
    )
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--instance-id", type=UUID, required=True)
    parser.add_argument("--startup-fd", type=int)
    arguments = parser.parse_args()
    # Fail before location discovery/state access for absent/invalid inherited authority.
    if (
        arguments.lock_fd < 3
        or arguments.startup_fd is None
        or arguments.startup_fd < 3
        or arguments.startup_fd == arguments.lock_fd
    ):
        return 1
    try:
        os.fstat(arguments.lock_fd)
        if (
            not stat.S_ISFIFO(os.fstat(arguments.startup_fd).st_mode)
            or fcntl.fcntl(arguments.startup_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY
        ):
            return 1
        os.umask(0o177)
        store = RuntimeStateStore(_selected_layout())
        owner = store.adopt(arguments.lock_fd)
        asyncio.run(
            _serve(
                owner,
                store.read_credential(),
                RuntimeIdentityData(
                    instance_id=arguments.instance_id,
                    pid=os.getpid(),
                ),
                arguments.startup_fd,
            )
        )
        # Intentionally hold the raw inherited lock FD until kernel process exit, not just
        # until the socket closes. No Python finalizer closes/unlocks the lock description.
        return 0
    except (ControlFault, StorageFault, OSError, ValueError):
        return 1  # no raw exceptions/credentials/frames on stderr


if __name__ == "__main__":
    raise SystemExit(main())
