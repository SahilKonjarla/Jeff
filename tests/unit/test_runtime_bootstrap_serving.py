"""In-process serving/drain orchestration with fake actors and a local diagnostic pipe."""

import asyncio
import os
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from jeff.application.runtime import RuntimeIdentityData, StartupResult
from jeff.application.storage import StorageFault, StorageReadiness
from jeff.bootstrap import runtime
from jeff.infrastructure.runtime_state import Ownership


@pytest.mark.parametrize(
    "mode",
    [
        "ready",
        "storage_failed",
        "initialization_failed",
        "initialization_unknown",
        "drain_failed",
        "broken_pipe",
    ],
)
def test_serving_keeps_captured_lifetimes_and_sql_final_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    events: list[str] = []
    activation: list[dict[str, object]] = []
    identity = RuntimeIdentityData(instance_id=UUID(int=1), pid=1)
    reader, writer = os.pipe()
    anchor = os.open(tmp_path / "synthetic-lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    closes: list[int] = []
    original_close, original_write = os.close, os.write

    class Owner:
        fd = anchor

        def check(self) -> None:
            events.append("owner.check")

    owner = cast(Ownership, Owner())

    class Events:
        def __init__(
            self,
            observed: RuntimeIdentityData,
            sender: object,
            health: Callable[[], bool],
            position: Callable[[], object],
        ) -> None:
            assert observed is identity and sender is runtime._stream_send
            self.health, self.position = health, position
            events.append("events.construct")
            # Do not evaluate these callbacks before storage exists.

        def signal(self) -> None:
            events.append("hint")

        def start(self, dispatcher: object) -> None:
            assert self.health() and self.position() is None
            events.append("events.start")
            servers[0].request_stop()

    class Storage:
        execution_profiles: tuple[object, ...] = ()
        execution_backend = object()
        notification_healthy = True

        def __init__(
            self,
            observed: Ownership,
            *,
            instance_id: UUID,
            commit_hint: object,
            scheduled_retention: bool,
        ) -> None:
            assert observed is owner and instance_id == identity.instance_id and scheduled_retention
            assert callable(commit_hint)
            self.closed = False
            storage_instances.append(self)
            events.append("sql.construct")

        def snapshot(self) -> StorageReadiness:
            return (
                StorageReadiness(state="failed", error="storage_failed")
                if mode == "storage_failed"
                else StorageReadiness(
                    state="ready",
                    schema_version=10,
                    retention_days=30,
                    settings_source="existing_file",
                )
            )

        def retention_position(self) -> None:
            return None

        def pause_retention(self) -> None:
            pass

        def start(self) -> None:
            events.append("sql.start")

        def request_close(self) -> None:
            events.append("sql.seal")
            asyncio.get_running_loop().call_soon(setattr, self, "closed", True)

        def close(self, *, timeout: float) -> None:
            assert timeout == 0 and self.closed
            events.append("sql.close")

    class Git:
        def __init__(self, duplicate: Callable[[], int]) -> None:
            self.duplicate = duplicate
            self.polls = 0
            git_instances.append(self)

        @property
        def closed(self) -> bool:
            self.polls += 1
            events.append("git.poll")
            return self.polls > 1

    class Server:
        def __init__(
            self,
            observed: Ownership,
            token: bytes,
            observed_identity: RuntimeIdentityData,
            snapshot: object,
            *,
            on_stop: object,
        ) -> None:
            assert observed is owner and token == bytes(32) and observed_identity is identity
            self.stopping = False
            self.finished = asyncio.Event()
            servers.append(self)

        async def serve(self) -> None:
            events.append("server.serve")
            await self.finished.wait()
            events.append("server.exit")

        def request_stop(self) -> None:
            self.stopping = True
            self.finished.set()
            events.append("server.stop")

        def check_stop_budget(self) -> None:
            events.append("budget")
            if mode == "drain_failed" and "sql.seal" not in events:
                raise runtime.ControlFault("runtime_stop_unconfirmed")

    class Dispatcher:
        def __init__(self, *args: object, fresh_factory: Callable[[], object] | None) -> None:
            assert fresh_factory is not None
            self.fresh_factory = fresh_factory
            self.initialization: Future[None] = Future()
            self.closed = False

        def start(self) -> None:
            events.append("dispatcher.start")
            self.fresh_factory()
            if mode == "initialization_failed":
                self.initialization.set_exception(StorageFault("storage_failed"))
            elif mode == "initialization_unknown":
                self.initialization.set_exception(ValueError("SYNTHETIC_INITIALIZATION"))
            else:
                self.initialization.set_result(None)

        def request_close(self) -> None:
            events.append("dispatcher.seal")
            asyncio.get_running_loop().call_soon(setattr, self, "closed", True)

        def close(self, *, timeout: float) -> None:
            assert timeout == 0 and self.closed
            events.append("dispatcher.close")

    storage_instances: list[Storage] = []
    servers: list[Server] = []
    git_instances: list[Git] = []

    def activate(**kwargs: object) -> object:
        assert kwargs["owner"] is owner and kwargs["storage"] is storage_instances[0]
        assert kwargs["instance_id"] == identity.instance_id
        activation.append(kwargs)
        return object()

    def close(fd: int) -> None:
        if fd == writer:
            closes.append(fd)
        original_close(fd)

    def write(fd: int, body: bytes) -> int:
        if fd == writer and mode == "broken_pipe":
            raise BrokenPipeError("SYNTHETIC_PIPE")
        return original_write(fd, body)

    for name in (
        "GitProjectDiscovery",
        "FileProjectStore",
        "ProjectService",
        "GitRepositoryRead",
        "QuestionService",
        "ProjectDispatcher",
        "ExecutionService",
    ):
        monkeypatch.setattr(runtime, name, lambda *args, **kwargs: object())
    for name, value in (
        ("RuntimeEvents", Events),
        ("SQLiteStorage", Storage),
        ("OwnedGitRunner", Git),
        ("ControlServer", Server),
        ("RuntimeDispatcher", Dispatcher),
        ("create_configured_execution", activate),
    ):
        monkeypatch.setattr(runtime, name, value)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(os, "write", write)
    began = time.monotonic()
    try:
        if mode in ("initialization_failed", "initialization_unknown", "drain_failed"):
            with pytest.raises((StorageFault, runtime.ControlFault)):
                asyncio.run(runtime._serve(owner, bytes(32), identity, writer))
        else:
            asyncio.run(runtime._serve(owner, bytes(32), identity, writer))
        ended = time.monotonic()
        assert closes == [writer]
        assert events[-1] == "sql.close"
        assert events.index("events.construct") < events.index("sql.construct")
        if activation:
            assert (
                began + runtime.DEFAULT_RUNTIME_LIMITS.start_seconds
                <= cast(float, activation[0]["deadline"])
                <= ended + runtime.DEFAULT_RUNTIME_LIMITS.start_seconds
            )
        if mode not in ("storage_failed", "drain_failed"):
            assert (
                events.index("dispatcher.close")
                < events.index("git.poll")
                < events.index("sql.seal")
            )
        if mode == "drain_failed":
            assert "sql.seal" in events and "dispatcher.close" not in events
        payload = os.read(reader, 512)
        if mode in ("initialization_failed", "initialization_unknown", "broken_pipe"):
            assert payload == b""
        else:
            report = runtime.StartupResult.model_validate_json(payload[4:])
            assert isinstance(report, StartupResult) and report.instance_id == identity.instance_id
        duplicate = git_instances[0].duplicate()
        try:
            assert os.fstat(duplicate).st_ino == os.fstat(anchor).st_ino
        finally:
            os.close(duplicate)
    finally:
        if not closes:
            os.close(writer)
        os.close(reader)
        os.close(anchor)
