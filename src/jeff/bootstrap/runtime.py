"""Internal control child composition; not a public CLI lifecycle command."""

import argparse
import asyncio
import fcntl
import os
import stat
import sys
from pathlib import Path
from typing import cast
from uuid import UUID

from jeff.application.dispatch import (
    ActivityClient,
    ProjectClient,
    ProjectDispatcher,
    ProjectStatusService,
)
from jeff.application.projects import Failure, ProjectService
from jeff.application.questions import QuestionService
from jeff.application.runtime import (
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
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_events import RuntimeEvents
from jeff.infrastructure.runtime_ipc import (
    ControlServer,
    RuntimeProjectClient,
    _stream_send,
    encode_message,
)
from jeff.infrastructure.runtime_paths import resolve_layout
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead


def _selected_layout() -> RuntimeLayout:
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


def create_activity_client() -> ActivityClient:
    """Lazy installed subscription interface; no CLI/auto-start/cursor persistence."""
    return RuntimeProjectClient(_selected_layout)


async def _serve(
    owner: Ownership, token: bytes, identity: RuntimeIdentityData, result_fd: int
) -> None:
    events = RuntimeEvents(
        identity,
        _stream_send,
        lambda: storage.snapshot().state == "ready" and storage.notification_healthy,
    )
    storage = SQLiteStorage(owner, instance_id=identity.instance_id, commit_hint=events.signal)
    dispatcher: RuntimeDispatcher | None = None
    server = ControlServer(owner, token, identity, storage.snapshot)
    server.events = events
    serving = asyncio.create_task(server.serve())
    try:
        storage.start()
        while storage.snapshot().state == "initializing":
            if serving.done():
                await serving
            await asyncio.sleep(0.01)
        state = storage.snapshot()
        if state.state == "ready" and not server.stopping:
            projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
            dispatcher = RuntimeDispatcher(
                ProjectDispatcher(
                    projects, QuestionService(projects, GitRepositoryRead()), storage, identity
                )
            )
            dispatcher.start()
            server.dispatcher = dispatcher
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
            # Native work/threads close before SQL and raw lifetime ownership can end.
            if dispatcher is not None:
                dispatcher.request_close()
                while not dispatcher.closed:
                    await asyncio.sleep(0.01)
                dispatcher.close(timeout=0)
            # Cooperative observation, not an executor/cancelled wait mistaken for SQL exit.
            storage.request_close()
            while not storage.closed:
                await asyncio.sleep(0.01)
            storage.close(timeout=0)


def main() -> int:
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
