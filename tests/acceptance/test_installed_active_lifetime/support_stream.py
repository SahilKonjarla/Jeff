"""Original live-reader lifetime and healthy/backpressured/retirement owner-death window."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from jeff.application.dispatch import ActivityFault, EvidenceQuery, ProjectCall, SubscriptionQuery
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import OwnedActivityStream, RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController

from .support_stream_programs import _BACKPRESSURE_HOOK, _RETIREMENT_HOOK, _STREAM_BOOT


@contextmanager
def _live_peer_reader(stream: OwnedActivityStream) -> Iterator[tuple[set[str], threading.Event]]:
    """Drain the healthy peer so another reader's backpressure remains isolated.

    Close the owned stream before joining; propagate reader failures to the test
    rather than treating a stopped thread as proof of successful peer shutdown.
    """
    seen: set[str] = set()
    closed, stop = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def read() -> None:
        try:
            while not stop.is_set():
                try:
                    frame = stream.receive(wait_seconds=0.1)
                except ControlFault as error:
                    assert error.code == "activity_stream_closed"
                    closed.set()
                    return
                if frame is not None and frame.kind == "closed":
                    closed.set()
                    return
                if (
                    frame is not None
                    and frame.event is not None
                    and frame.event.kind == "request.succeeded"
                ):
                    seen.add(str(frame.event.request_id))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=read)
    thread.start()
    try:
        yield seen, closed
    finally:
        stop.set()
        stream.close()
        thread.join(5)
        assert not thread.is_alive() and not errors


def _healthy_stream_window(
    tmp_path_factory: pytest.TempPathFactory,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    from contextlib import ExitStack

    root = tmp_path_factory.mktemp("s").resolve()
    gate = root / "gate"
    gate.mkdir(mode=0o700)
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    original = subprocess.Popen
    slow_query = SubscriptionQuery(directory=git_repo)
    backpressured = window == "backpressured_stream"
    retiring = window.startswith("retirement_")
    hook = _BACKPRESSURE_HOOK if backpressured else _RETIREMENT_HOOK if retiring else ""
    script = hook + "\n" + _STREAM_BOOT

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["env"] = {
            **kwargs["env"],
            "JEFF_TEST_D6_GATE": str(gate),
            "JEFF_TEST_D6_SLOW_ID": str(slow_query.query_id),
            "JEFF_TEST_D6_WINDOW": window,
        }
        return original([*command[:3], "-c", script, *command[5:]], **kwargs)

    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None
        captured = control._child
        assert captured is not None
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        with (
            client.subscribe(slow_query) as stream,
            client.subscribe(SubscriptionQuery(directory=git_repo)) as second,
            _live_peer_reader(second) as (seen, healthy_closed),
        ):
            if backpressured:
                stream.connection.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            if retiring:
                (gate / "buffer").touch(mode=0o600)
            for _ in range(40 if backpressured else 1):
                call = ProjectCall(
                    operation="ask", directory=git_repo, question="what branch am I on?"
                )
                completed = client.execute(call)
                assert completed.result.ok and completed.receipt is not None
                if not backpressured or (gate / "blocked").exists():
                    break
            if backpressured:
                assert (gate / "blocked").exists(), "Actual kernel-backed drain must be pending"
                assert not (gate / "unwound").exists()
            elif retiring:
                until = time.monotonic() + 5
                while not (gate / "buffered").exists():
                    assert time.monotonic() < until
                    time.sleep(0.01)
                frame = stream.receive(wait_seconds=1)
                assert frame is not None and frame.event is not None
            else:
                until = time.monotonic() + 5
                while True:
                    assert time.monotonic() < until
                    frame = stream.receive(wait_seconds=0.5)
                    if frame is not None and frame.event is not None:
                        if (
                            frame.event.request_id == call.request_id
                            and frame.event.kind == "request.succeeded"
                        ):
                            break
            until = time.monotonic() + 3
            while str(call.request_id) not in seen:
                assert time.monotonic() < until and not healthy_closed.is_set()
                time.sleep(0.01)
            cursor = stream.last_received_cursor
            assert isinstance(control.status(), ControlSuccess)
            retirement_facts: dict[str, Any] = {}
            if retiring:
                (gate / "expire").touch(mode=0o600)
                until = time.monotonic() + 5
                while not (gate / "retire-ready").exists():
                    assert captured.poll() is None and time.monotonic() < until
                    time.sleep(0.01)
                retirement_facts = json.loads((gate / "retire-ready").read_text())
                committed = window == "retirement_after_commit"
                assert retirement_facts == {
                    "in_transaction": not committed,
                    "journal_magic": "" if committed else "d9d505f920a163d7",
                    "permit_present": not committed,
                }
            (gate / "seal").touch(mode=0o600)
            until = time.monotonic() + 5
            while not (gate / "quiescent").exists():
                assert captured.poll() is None and time.monotonic() < until
                time.sleep(0.01)
            facts = json.loads((gate / "quiescent").read_text())
            assert facts == {"active_git_calls": 0, "helpers_closed": True}
            (root / "actor-observation.json").write_text(
                json.dumps(
                    {
                        "window": window,
                        "caught_up_terminal": not (backpressured or retiring),
                        "healthy_second_peer": True,
                        "healthy_closed_before_death": healthy_closed.is_set(),
                        "actual_backpressure": backpressured,
                        "buffered_page": retiring,
                        "retirement": retirement_facts,
                        **facts,
                    }
                )
            )
            if os.environ.get("JEFF_TEST_D6_PREFLIGHT") == "1":
                (gate / "release").touch(mode=0o600)
                (gate / "seal").unlink()
                return
            fact = {
                "role": "daemon",
                "signal": "SIGKILL",
                "window": window,
                "execution": os.environ["JEFF_TEST_D6_EXECUTION"],
                "captured_pid": captured.pid,
            }
            log = root / "crash-execution.json"
            with log.open("x") as out:
                json.dump(fact, out)
            if backpressured:
                assert not (gate / "unwound").exists()
            captured.kill()
            assert captured.wait(timeout=5) == -9
            fact["native_exit_code"] = -9
            log.write_text(json.dumps(fact))
            assert healthy_closed.wait(3) and second.connection.connection.fileno() == -1
            # Bytes already sent may remain; drain them honestly before bounded closure.
            for owned in (stream,):
                until = time.monotonic() + 3
                for _ in range(128):
                    assert time.monotonic() < until
                    try:
                        frame = owned.receive(wait_seconds=0.1)
                    except ControlFault as error:
                        assert error.code == "activity_stream_closed"
                        break
                    if frame is not None and frame.kind == "closed":
                        break
                else:
                    pytest.fail("Captured reader did not close under original bound")
                assert owned.connection.connection.fileno() == -1
        (gate / "seal").unlink()
        restarted = control.start()
        assert isinstance(restarted, ControlSuccess) and restarted.data.identity is not None
        assert restarted.data.identity.instance_id != started.data.identity.instance_id
        trace = client.inspect_evidence(
            EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
        )
        retired = window == "retirement_after_commit"
        if retired:
            assert trace.error is None and trace.receipt is None and trace.trace is None
            assert trace.missing_is_not_nonexecution_proof
        else:
            assert trace.receipt == completed.receipt
        duplicate = client.execute(call)
        assert not duplicate.result.ok
        assert duplicate.result.error.code == (
            "request_retired" if retired else "duplicate_request"
        )
        with ExitStack() as streams:
            if retired:
                with pytest.raises(ActivityFault) as invalid:
                    client.subscribe(SubscriptionQuery(directory=git_repo, cursor=cursor))
                assert invalid.value.code == "activity_invalid_cursor"
                resumed = streams.enter_context(
                    client.subscribe(SubscriptionQuery(directory=git_repo))
                )
            else:
                resumed = streams.enter_context(
                    client.subscribe(SubscriptionQuery(directory=git_repo, cursor=cursor))
                )
                assert resumed.ready.cursor == cursor
            for _ in range(7):
                fresh = streams.enter_context(
                    client.subscribe(SubscriptionQuery(directory=git_repo))
                )
                assert fresh.ready.metadata is not None
            assert isinstance(control.status(), ControlSuccess)
    finally:
        (gate / "release").touch(mode=0o600)
        (gate / "seal").unlink(missing_ok=True)
        assert isinstance(control.stop(), ControlSuccess)
