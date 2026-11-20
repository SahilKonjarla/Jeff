"""Scheduled retirement invalidates real buffered/backpressured writers and frees slots."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.dispatch import ActivityFault, ProjectCall, SubscriptionQuery
from jeff.application.history import StreamCursor
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


@pytest.mark.parametrize("suppress_hint", [False, True])
def test_retirement_invalidates_buffered_writer_while_global_read_is_busy(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suppress_hint: bool,
) -> None:
    other = tmp_path / "b"
    subprocess.run(
        ["/usr/bin/git", "-c", "init.templateDir=", "init", "--initial-branch=main", str(other)],
        env={"PATH": os.defpath, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        check=True,
        timeout=5,
    )
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    slow_id = uuid4()
    sending, read_busy = tmp_path / "sending", tmp_path / "read-busy"
    progress_queued = tmp_path / "progress-queued"
    published = tmp_path / "retirement-published"
    arm_read, expire, committed = tmp_path / "arm-read", tmp_path / "expire", tmp_path / "committed"
    release_send, release_read = tmp_path / "release-send", tmp_path / "release-read"
    original = subprocess.Popen
    # Install SQL/socket hooks before bootstrap captures them. Inject clocks only at
    # composition; rebinding the storage class would change the fixture's type identity.
    launcher = f"""
import asyncio,sqlite3,subprocess,time
from pathlib import Path
import jeff.infrastructure.runtime_storage as sql
import jeff.infrastructure.runtime_ipc as ipc
expire=Path({str(expire)!r})
original_store,original_connect,original_send=sql.SQLiteStorage,sqlite3.connect,ipc._stream_send
def store(*args,**kwargs):
    kwargs['retention_clock']=lambda:time.time_ns()//1000000+(31*86400000 if expire.exists() else 0)
    kwargs['retention_monotonic']=lambda:160.0 if expire.exists() else 100.0
    assert kwargs['scheduled_retention'] is True  # actual bootstrap opt-in; clocks alone vary
    if {suppress_hint!r}:kwargs['commit_hint']=lambda:None
    storage=original_store(*args,**kwargs)
    original_publish=storage._publish_retention
    def publish(meta):
        original_publish(meta)
        if expire.exists() and meta.replay_floor>0:
            Path({str(published)!r}).write_text('checked retirement cache published')
    storage._publish_retention=publish
    return storage
class Observed(sqlite3.Connection):
    retiring=False
    def execute(self,statement,parameters=()):
        if statement.startswith('INSERT INTO request_tombstones'):self.retiring=True
        result=super().execute(statement,parameters)
        if statement=='COMMIT' and self.retiring:Path({str(committed)!r}).write_text('committed')
        return result
def connect(*args,**kwargs):return original_connect(*args,**kwargs,factory=Observed)
sqlite3.connect=connect
async def send(writer,frame):
    await original_send(writer,frame)
    if str(frame.request_id)!={str(slow_id)!r} and frame.kind=='progress' and not expire.exists():
        with Path({str(progress_queued)!r}).open('a') as output:output.write(frame.cursor+'\\n')
    if str(frame.request_id)=={str(slow_id)!r} and frame.kind=='event':
        Path({str(sending)!r}).write_text('real prefix written')
        until=time.monotonic()+8
        while not Path({str(progress_queued)!r}).exists():
            if time.monotonic()>until:raise TimeoutError('Owned fixture progress gate expired')
            await asyncio.sleep(.01)
        Path({str(arm_read)!r}).write_text('arm after actual buffered event and progress')
        while not Path({str(release_send)!r}).exists():
            if time.monotonic()>until:raise TimeoutError('Owned fixture write gate expired')
            await asyncio.sleep(.01)
ipc._stream_send=send
import jeff.bootstrap.runtime as runtime  # Import after actual SQL/socket hooks.
original_runner=runtime.OwnedGitRunner
class GatedRunner(original_runner):
    def __call__(self,executable,directory,*args,**kwargs):
        if directory==Path({str(other)!r}) and Path({str(arm_read)!r}).exists():
            Path({str(read_busy)!r}).write_text('worker held before owned Git spawn')
            until=time.monotonic()+8
            while not Path({str(release_read)!r}).exists():
                if time.monotonic()>until:raise OSError('Owned fixture read gate expired')
                time.sleep(.01)
        return super().__call__(executable,directory,*args,**kwargs)
runtime.OwnedGitRunner=GatedRunner
runtime.SQLiteStorage=store
raise SystemExit(runtime.main())
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
        assert client.execute(ProjectCall(operation="init", directory=other)).result.ok
        with (
            client.subscribe(SubscriptionQuery(directory=git_repo, query_id=slow_id)) as slow,
            client.subscribe(SubscriptionQuery(directory=other)) as second,
        ):
            assert client.execute(
                ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
            ).result.ok
            until = time.monotonic() + 3
            while not (sending.exists() and read_busy.exists()):
                assert time.monotonic() < until
                time.sleep(0.01)
            prefix = slow.receive(wait_seconds=1)
            assert prefix is not None and prefix.event is not None
            old_cursor = slow.last_received_cursor
            other_position = StreamCursor.decode(second.last_received_cursor)
            queued_progress = set(progress_queued.read_text().splitlines())
            assert queued_progress
            expire.write_text("advance owned clocks")
            until = time.monotonic() + 3
            while not (committed.exists() and published.exists()):
                assert time.monotonic() < until
                time.sleep(0.01)
            # A delivered hint aborts immediately even with the global read held. If hints
            # are dropped, the suspended real write's completion must check the cached floor.
            if suppress_hint:
                release_send.write_text("release")
            with pytest.raises(ControlFault) as closed:
                slow.receive(wait_seconds=2)
            assert closed.value.code == "activity_stream_closed"
            if suppress_hint:
                release_read.write_text("release")
            until = time.monotonic() + 2
            while True:
                remaining = until - time.monotonic()
                assert remaining > 0, "Other stream did not close within two seconds"
                other_closed = second.receive(wait_seconds=remaining)
                assert other_closed is not None
                if other_closed.kind == "closed":
                    break
                # Only bytes actually written before retirement may precede closure.
                assert other_closed.kind == "progress" and other_closed.cursor in queued_progress
                assert other_closed.cursor is not None
                position = StreamCursor.decode(other_closed.cursor)
                assert position.database_epoch == other_position.database_epoch
                assert position.retention_generation == other_position.retention_generation
            assert time.monotonic() <= until
            assert other_closed.error is not None
            assert other_closed.error.error.code == "activity_invalid_cursor"
            if not suppress_hint:
                assert not release_send.exists() and not release_read.exists()
            release_send.write_text("release")
            release_read.write_text("release")
            with pytest.raises(ActivityFault) as resync:
                client.subscribe(SubscriptionQuery(directory=git_repo, cursor=old_cursor))
            assert resync.value.code == "activity_invalid_cursor"
            with client.subscribe(SubscriptionQuery(directory=git_repo)) as fresh:
                assert fresh.ready.metadata is not None and fresh.ready.metadata.replay_floor > 0
            assert isinstance(control.status(), ControlSuccess)
    finally:
        release_send.write_text("release")
        release_read.write_text("release")
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_retirement_aborts_real_kernel_backpressure_and_releases_server_slots(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from jeff.application.dispatch import StreamFrame, SubscriptionRequest
    from jeff.infrastructure.runtime_ipc import ControlClient, encode_message, receive_message
    from jeff.infrastructure.runtime_state import RuntimeStateStore

    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    expire, committed = tmp_path / "expire", tmp_path / "committed"
    blocked, released = tmp_path / "blocked", tmp_path / "released"
    query = SubscriptionQuery(directory=git_repo)
    original = subprocess.Popen
    launcher = f"""
import asyncio,socket,sqlite3,time
from pathlib import Path
import jeff.infrastructure.runtime_storage as sql
import jeff.infrastructure.runtime_ipc as ipc
original_store,original_connect,original_send=sql.SQLiteStorage,sqlite3.connect,ipc._stream_send
expire=Path({str(expire)!r})
def store(*args,**kwargs):
    kwargs['retention_clock']=lambda:time.time_ns()//1000000+(31*86400000 if expire.exists() else 0)
    kwargs['retention_monotonic']=lambda:160.0 if expire.exists() else 100.0
    assert kwargs['scheduled_retention'] is True
    return original_store(*args,**kwargs)
class Observed(sqlite3.Connection):
    retiring=False
    def execute(self,statement,parameters=()):
        if statement.startswith('INSERT INTO request_tombstones'):self.retiring=True
        result=super().execute(statement,parameters)
        if statement=='COMMIT' and self.retiring:Path({str(committed)!r}).write_text('committed')
        return result
def connect(*args,**kwargs):return original_connect(*args,**kwargs,factory=Observed)
sqlite3.connect=connect
async def send(writer,frame):
    if str(frame.request_id)!={str(query.query_id)!r}:return await original_send(writer,frame)
    writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
    actual=asyncio.create_task(original_send(writer,frame))
    await asyncio.sleep(0)  # observe actual write/drain, never a fake writer or fake result
    waiting=writer.transport.get_write_buffer_size()>65536 and not actual.done()
    if waiting:Path({str(blocked)!r}).write_text('real high-water backpressure')
    try:await actual
    finally:
        if waiting:Path({str(released)!r}).write_text('real send unwound')
ipc._stream_send=send
import jeff.bootstrap.runtime as runtime  # Capture the real patched stream sender at composition.
runtime.SQLiteStorage=store
raise SystemExit(runtime.main())
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
        with ControlClient(layout, token).connect(time.monotonic() + 5) as raw:
            raw.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            request = SubscriptionRequest(
                query=query, expected_instance_id=raw.identity.instance_id
            )
            raw.connection.sendall(encode_message(request.model_dump(mode="json"), 65536))
            ready = receive_message(raw.connection, 65536, time.monotonic() + 5, StreamFrame)
            assert ready.metadata is not None
            for _ in range(40):
                assert client.execute(
                    ProjectCall(
                        operation="ask", directory=git_repo, question="what branch am I on?"
                    )
                ).result.ok
                if blocked.exists():
                    break
            assert blocked.exists(), "Owned native writer must actually cross its64KiB high-water"
            began = time.monotonic()
            expire.write_text("advance clocks")
            until = began + 3
            while not (committed.exists() and released.exists()):
                assert time.monotonic() < until
                time.sleep(0.01)
            assert time.monotonic() - began < 4  # not the ordinary5s slow-reader timeout
            with ExitStack() as streams:
                for _ in range(8):
                    fresh = streams.enter_context(
                        client.subscribe(SubscriptionQuery(directory=git_repo))
                    )
                    assert fresh.ready.metadata is not None
                    assert (
                        fresh.ready.metadata.retention_generation
                        > ready.metadata.retention_generation
                    )
                # Actual server slots are released while raw still holds its unread client socket.
                assert isinstance(control.stop(), ControlSuccess)
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
