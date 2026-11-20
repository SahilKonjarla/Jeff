"""Real reader capacity, scope/close interruption, kernel backpressure and malformed frames."""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.dispatch import ActivityFault, ProjectCall, SubscriptionQuery
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


def test_eight_slots_idle_close_interrupt_and_scope_refusal(git_repo: Path, tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    try:
        assert isinstance(control.start(), ControlSuccess)
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        with ExitStack() as streams:
            opened = [
                streams.enter_context(client.subscribe(SubscriptionQuery(directory=git_repo)))
                for _ in range(8)
            ]
            with pytest.raises(ActivityFault) as busy:
                client.subscribe(SubscriptionQuery(directory=git_repo))
            assert busy.value.code == "runtime_busy"
            assert isinstance(control.status(), ControlSuccess)
            assert opened[0].receive(wait_seconds=5.1) is None  # healthy idle exceeds frame budget
            with ThreadPoolExecutor(max_workers=1) as worker:
                waiting = worker.submit(opened[0].receive)
                time.sleep(0.05)
                opened[0].close()
                with pytest.raises(ControlFault):
                    waiting.result(timeout=2)
        with pytest.raises(ActivityFault) as forged:
            client.subscribe(SubscriptionQuery(directory=git_repo, expected_project_id=uuid4()))
        assert forged.value.code == "project_target_mismatch"
        with client.subscribe(SubscriptionQuery(directory=git_repo)) as stream:
            (git_repo / "jeff.local.yaml").unlink()
            message = stream.receive(wait_seconds=3)
            assert message is not None and message.kind == "closed"
            assert message.error is not None
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_native_slow_reader_closes_without_blocking_second_client(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    from jeff.application.dispatch import StreamFrame, SubscriptionRequest
    from jeff.infrastructure.runtime_ipc import ControlClient, encode_message, receive_message
    from jeff.infrastructure.runtime_state import RuntimeStateStore

    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    original = subprocess.Popen
    # Larger truthful root fields fill the real transport without30 redundant Git calls.
    padding = tmp_path / "event-root"
    while len(os.fsencode(padding)) < 800:
        padding /= "r" * 48
    padding.mkdir(parents=True, mode=0o700)
    destination = padding / "repo"
    git_repo.rename(destination)
    git_repo = destination
    blocked = tmp_path / "actual-backpressure"
    # Actual owned socket buffers/drain, unchanged original5-second send deadline.
    launcher = f"""
import asyncio,runpy,socket
from pathlib import Path
import jeff.infrastructure.runtime_ipc as ipc
original=ipc._stream_send
async def send(writer,frame):
    writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
    task=asyncio.create_task(original(writer,frame))
    await asyncio.sleep(0)
    buffered=writer.transport.get_write_buffer_size()
    high=writer.transport.get_write_buffer_limits()[1]
    if frame.kind=='event'and buffered>high:
        Path({str(blocked)!r}).touch(mode=0o600)
    return await task
ipc._stream_send=send
runpy.run_module('jeff.bootstrap.runtime',run_name='__main__')
"""

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if "jeff.bootstrap.runtime" in command:
            return original([*command[:3], "-c", launcher, *command[5:]], **kwargs)
        return original(command, **kwargs)

    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            assert isinstance(control.start(), ControlSuccess)
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        token = RuntimeStateStore(layout).read_credential()
        with ControlClient(layout, token).connect(time.monotonic() + 10) as slow:
            query = SubscriptionQuery(directory=git_repo)
            request = SubscriptionRequest(
                query=query, expected_instance_id=slow.identity.instance_id
            )
            slow.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            slow.connection.sendall(encode_message(request.model_dump(mode="json"), 65536))
            ready = receive_message(slow.connection, 65536, time.monotonic() + 5, StreamFrame)
            assert ready.kind == "ready"
            for _ in range(20):
                assert client.execute(
                    ProjectCall(
                        operation="ask", directory=git_repo, question="what branch am I on?"
                    )
                ).result.ok
                if blocked.exists():
                    break
            assert blocked.exists(), "Actual socket/transport backpressure was not reached"
            assert isinstance(control.status(), ControlSuccess)
            time.sleep(5.3)
            # All8 slots now reusable while the unconsuming peer still holds its own socket.
            with ExitStack() as opened:
                for _ in range(8):
                    opened.enter_context(client.subscribe(SubscriptionQuery(directory=git_repo)))
                assert isinstance(control.status(), ControlSuccess)
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


@pytest.mark.parametrize("oversized", [False, True])
def test_partial_or_oversized_native_stream_frame_closes_only_its_reader(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oversized: bool
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    original = subprocess.Popen
    launcher = f"""
import asyncio,runpy,struct
import jeff.infrastructure.runtime_ipc as ipc
original=ipc._stream_send
async def send(writer,frame):
    if frame.kind!='event':return await original(writer,frame)
    writer.write(struct.pack('!I',4294967295 if {oversized!r} else 100)+b'{{')
    await writer.drain()
    await asyncio.sleep(7)
ipc._stream_send=send
runpy.run_module('jeff.bootstrap.runtime',run_name='__main__')
"""

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if "jeff.bootstrap.runtime" in command:
            return original([*command[:3], "-c", launcher, *command[5:]], **kwargs)
        return original(command, **kwargs)

    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            assert isinstance(control.start(), ControlSuccess)
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        with client.subscribe(SubscriptionQuery(directory=git_repo)) as stream:
            assert client.execute(
                ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
            ).result.ok
            began = time.monotonic()
            with pytest.raises(ControlFault):
                stream.receive(wait_seconds=3)
            elapsed = time.monotonic() - began
            if oversized:
                assert elapsed < 2
            else:
                assert 4.5 <= elapsed < 7
            assert isinstance(control.status(), ControlSuccess)
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
