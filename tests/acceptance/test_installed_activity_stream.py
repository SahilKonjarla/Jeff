"""Independent authenticated activity client observes real CLI work, no action replay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.dispatch import ActivityFault, ProjectCall, SubscriptionQuery
from jeff.application.projects import ProjectService
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.integrations.git import GitProjectDiscovery

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


@pytest.mark.parametrize("suppress_hint", [False, True])
def test_live_cli_events_then_explicit_resume_without_reexecution(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suppress_hint: bool
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    try:
        original = subprocess.Popen
        launcher = """
import runpy
from jeff.infrastructure.runtime_events import RuntimeEvents
RuntimeEvents.signal=lambda self:None
runpy.run_module('jeff.bootstrap.runtime',run_name='__main__')
"""

        def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            if suppress_hint and "jeff.bootstrap.runtime" in command:
                return original([*command[:3], "-c", launcher, *command[5:]], **kwargs)
            return original(command, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess)
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        with client.subscribe(SubscriptionQuery(directory=git_repo)) as stream:
            cursor = stream.ready.cursor
            assert cursor is not None
            assert stream.receive(wait_seconds=0.05) is None
            env = {
                "PATH": os.defpath,
                "HOME": str(tmp_path),
                "JEFF_DATA_DIR": str(layout.data_dir),
                "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
                "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            child = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "jeff", "ask", "what branch am I on?", "--json"],
                cwd=git_repo,
                env=env,
                capture_output=True,
                timeout=10,
            )
            assert child.returncode == 0, "Owned CLI failed; raw output suppressed"
            events = []
            for _ in range(10):
                message = stream.receive(wait_seconds=3)
                assert message is not None and message.kind != "closed"
                if message.event is not None:
                    events.append(message.event)
                    if message.event.kind == "request.succeeded":
                        break
            assert [event.kind for event in events] == [
                "request.accepted",
                "request.running",
                "git.finished",
                "git.finished",
                "request.succeeded",
            ]
            assert len({event.request_id for event in events}) == 1
        before = layout.database.read_bytes()
        with client.subscribe(SubscriptionQuery(directory=git_repo, cursor=cursor)) as resumed:
            assert resumed.ready.cursor == cursor
            replay = []
            for _ in range(10):
                message = resumed.receive(wait_seconds=3)
                assert message is not None
                if message.event is not None:
                    replay.append(message.event)
                    if len(replay) == len(events):
                        break
            assert replay == events
        assert layout.database.read_bytes() == before
        with client.subscribe(SubscriptionQuery(directory=git_repo, cursor=cursor)) as batching:
            calls = [
                ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
                for _ in range(14)
            ]
            for call in calls:
                assert client.execute(call).result.ok
            expected = {events[0].request_id, *(call.request_id for call in calls)}
            completed = set()
            sequences = []
            for _ in range(100):
                message = batching.receive(wait_seconds=3)
                assert message is not None and message.kind != "closed"
                if message.event is not None:
                    sequences.append(message.event.sequence)
                    if message.event.kind == "request.succeeded":
                        completed.add(message.event.request_id)
                        if completed == expected:
                            break
            assert completed == expected and len(sequences) == 75
            assert all(a < b for a, b in zip(sequences, sequences[1:], strict=False))
        assert isinstance(control.stop(), ControlSuccess)
        assert isinstance(control.start(), ControlSuccess)
        with client.subscribe(SubscriptionQuery(directory=git_repo, cursor=cursor)) as restarted:
            first = restarted.receive(wait_seconds=3)
            assert first is not None and first.event == events[0]
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


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


@pytest.mark.parametrize("rollback,stopping", [(False, False), (True, False), (False, True)])
def test_actual_paused_commit_never_publishes_before_commit(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback: bool, stopping: bool
) -> None:
    assert ProjectService(GitProjectDiscovery(), FileProjectStore()).execute("init", git_repo).ok
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    entered, release = tmp_path / "entered", tmp_path / "release"
    original = subprocess.Popen
    launcher = f"""
import runpy,sqlite3,time
from pathlib import Path
original=sqlite3.connect
entered,release=Path({str(entered)!r}),Path({str(release)!r})
class Paused(sqlite3.Connection):
    armed=False
    once=False
    def execute(self,sql,parameters=()):
        if sql.startswith('INSERT INTO request_receipts'):self.armed=True
        if sql=='COMMIT' and self.armed and not self.once:
            self.once=True
            entered.write_text('entered')
            until=time.monotonic()+8
            while not release.exists():
                if time.monotonic()>until:raise sqlite3.OperationalError('Synthetic wait expired')
                time.sleep(.01)
            if {rollback!r}:raise sqlite3.OperationalError('Synthetic rollback')
        return super().execute(sql,parameters)
def connect(*args,**kwargs):return original(*args,**kwargs,factory=Paused)
sqlite3.connect=connect
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
        with (
            client.subscribe(SubscriptionQuery(directory=git_repo)) as stream,
            ExitStack() as spare,
        ):
            mixed = not rollback and not stopping
            raw_peers = []
            if mixed:
                for _ in range(7):
                    spare.enter_context(client.subscribe(SubscriptionQuery(directory=git_repo)))
            with ThreadPoolExecutor(max_workers=2) as worker:
                call = worker.submit(
                    client.execute,
                    ProjectCall(
                        operation="ask", directory=git_repo, question="what branch am I on?"
                    ),
                )
                try:
                    until = time.monotonic() + 3
                    while not entered.exists():
                        assert time.monotonic() < until
                        time.sleep(0.01)
                    assert stream.receive(wait_seconds=0.2) is None
                    assert isinstance(control.status(), ControlSuccess)
                    if mixed:
                        from jeff.application.dispatch import ProjectRequest
                        from jeff.infrastructure.runtime_ipc import ControlClient, encode_message
                        from jeff.infrastructure.runtime_state import RuntimeStateStore

                        token = RuntimeStateStore(layout).read_credential()
                        for _ in range(6):
                            peer = spare.enter_context(
                                ControlClient(layout, token).connect(time.monotonic() + 5)
                            )
                            wire = ProjectRequest(
                                call=ProjectCall(
                                    operation="ask",
                                    directory=git_repo,
                                    question="what branch am I on?",
                                ),
                                expected_instance_id=peer.identity.instance_id,
                            )
                            peer.connection.sendall(
                                encode_message(wire.model_dump(mode="json"), 65536)
                            )
                            raw_peers.append(peer)
                        # Same counter covers8 subscriptions plus7 admitted project requests.
                        refused = client.execute(
                            ProjectCall(
                                operation="ask", directory=git_repo, question="what branch am I on?"
                            )
                        )
                        assert not refused.result.ok and refused.result.error.code == "runtime_busy"
                        assert isinstance(control.status(), ControlSuccess)
                    if stopping:
                        stopped = worker.submit(control.stop)
                        from jeff.infrastructure.runtime_ipc import ControlClient
                        from jeff.infrastructure.runtime_state import RuntimeStateStore

                        native = ControlClient(layout, RuntimeStateStore(layout).read_credential())
                        until = time.monotonic() + 2
                        while True:
                            status = native.request("status", deadline=time.monotonic() + 1)
                            if status.state.value == "stopping":
                                break
                            assert time.monotonic() < until
                            time.sleep(0.01)
                finally:
                    release.write_text("release")
                result = call.result(timeout=5)
                if mixed:
                    from jeff.application.dispatch import DispatchReply
                    from jeff.infrastructure.runtime_ipc import receive_message

                    for peer in raw_peers:
                        reply = receive_message(
                            peer.connection, 16777216, time.monotonic() + 5, DispatchReply
                        )
                        assert reply.result.ok
                    # Stop with8 live streams, after all7 actions actually replied; don't
                    # confuse a client close request with released native/server capacity.
                    assert isinstance(control.stop(), ControlSuccess)
                if stopping:
                    assert isinstance(stopped.result(timeout=5), ControlSuccess)
            message = stream.receive(wait_seconds=3)
            assert message is not None
            if rollback:
                assert not result.result.ok and message.kind == "closed" and message.event is None
            else:
                assert result.result.ok and message.event is not None
                assert message.event.kind == "request.accepted"
                if stopping:
                    kinds: list[str] = [message.event.kind]
                    for _ in range(10):
                        message = stream.receive(wait_seconds=2)
                        assert message is not None
                        if message.kind == "closed":
                            break
                        if message.event is not None:
                            kinds.append(message.event.kind)
                    assert kinds[-1] == "request.succeeded" and message.kind == "closed"
    finally:
        release.write_text("release")
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_same_wire_b_capability_refusal_and_old_b_client_works(
    git_repo: Path, tmp_path: Path
) -> None:
    old = os.environ.get("JEFF_TEST_B_PYTHON")
    if old is None:
        pytest.skip("Requires verified retained B wheel")
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def legacy(operation: str) -> dict[str, Any]:
        code = """
import sys
from pathlib import Path
from jeff.bootstrap.runtime import create_runtime_controller,create_project_client
from jeff.application.dispatch import ProjectCall
from jeff.application.runtime import PROTOCOL_REVISION
assert PROTOCOL_REVISION==5
if sys.argv[1]=='init':
    r=create_project_client().execute(ProjectCall(operation='init',directory=Path(sys.argv[2])))
else:r=getattr(create_runtime_controller(),sys.argv[1])()
print(r.model_dump_json())
"""
        child = subprocess.run(
            [old, "-I", "-B", "-c", code, operation, str(git_repo)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            timeout=15,
        )
        assert child.returncode == 0, "Owned B helper failed; raw output suppressed"
        result: dict[str, Any] = json.loads(child.stdout)
        return result

    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    try:
        assert legacy("start")["ok"]
        before = layout.credential.stat().st_ino, layout.database.read_bytes()
        refused = control.start()
        assert not refused.ok and refused.error.code == "runtime_capability_unavailable"
        with pytest.raises(ControlFault) as missing:
            client.subscribe(SubscriptionQuery(directory=git_repo))
        assert missing.value.code == "runtime_capability_unavailable"
        assert before == (layout.credential.stat().st_ino, layout.database.read_bytes())
    finally:
        assert legacy("stop")["ok"]
    try:
        assert isinstance(control.start(), ControlSuccess)
        assert legacy("start")["ok"]
        assert legacy("init")["result"]["ok"]
        status = legacy("status")
        assert status["ok"] and "activity.subscribe" in status["data"]["available_operations"]
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
    # Actual owned socket buffer constraint, not a fake writer/drain or shortened timeout.
    launcher = """
import runpy,socket
import jeff.infrastructure.runtime_ipc as ipc
original=ipc._stream_send
async def send(writer,frame):
    writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
    return await original(writer,frame)
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
            for _ in range(30):
                assert client.execute(
                    ProjectCall(
                        operation="ask", directory=git_repo, question="what branch am I on?"
                    )
                ).result.ok
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
    arm_read, expire, committed = tmp_path / "arm-read", tmp_path / "expire", tmp_path / "committed"
    release_send, release_read = tmp_path / "release-send", tmp_path / "release-read"
    original = subprocess.Popen
    launcher = f"""
import asyncio,runpy,sqlite3,subprocess,time
from pathlib import Path
import jeff.infrastructure.runtime_storage as sql
import jeff.infrastructure.runtime_ipc as ipc
expire=Path({str(expire)!r})
original_store,original_connect,original_send,original_popen=sql.SQLiteStorage,sqlite3.connect,ipc._stream_send,subprocess.Popen
def store(*args,**kwargs):
    kwargs['retention_clock']=lambda:time.time_ns()//1000000+(31*86400000 if expire.exists() else 0)
    kwargs['retention_monotonic']=lambda:160.0 if expire.exists() else 100.0
    assert kwargs['scheduled_retention'] is True  # actual bootstrap opt-in; clocks alone vary
    if {suppress_hint!r}:kwargs['commit_hint']=lambda:None
    return original_store(*args,**kwargs)
sql.SQLiteStorage=store
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
    if str(frame.request_id)=={str(slow_id)!r} and frame.kind=='event':
        Path({str(sending)!r}).write_text('real prefix written')
        until=time.monotonic()+8
        while not Path({str(release_send)!r}).exists():
            if time.monotonic()>until:raise TimeoutError('Owned fixture write gate expired')
            await asyncio.sleep(.01)
ipc._stream_send=send
def popen(command,**kwargs):
    if ('-C' in command and command[command.index('-C')+1]=={str(other)!r}
        and Path({str(arm_read)!r}).exists()):
        Path({str(read_busy)!r}).write_text('worker held at Git process seam')
        until=time.monotonic()+8
        while not Path({str(release_read)!r}).exists():
            if time.monotonic()>until:raise OSError('Owned fixture read gate expired')
            time.sleep(.01)
    return original_popen(command,**kwargs)
subprocess.Popen=popen
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
        assert client.execute(ProjectCall(operation="init", directory=other)).result.ok
        with (
            client.subscribe(SubscriptionQuery(directory=git_repo, query_id=slow_id)) as slow,
            client.subscribe(SubscriptionQuery(directory=other)) as second,
        ):
            arm_read.write_text("arm")
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
            expire.write_text("advance owned clocks")
            until = time.monotonic() + 3
            while not committed.exists():
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
            other_closed = second.receive(wait_seconds=2)
            assert other_closed is not None and other_closed.kind == "closed"
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
import asyncio,runpy,socket,sqlite3,time
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
sql.SQLiteStorage=store
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
