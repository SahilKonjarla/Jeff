"""One actual request survives expiry only as durable native duplicate exclusion."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from jeff.application.dispatch import ActivityFault, HistoryQuery, ProjectCall, SubscriptionQuery
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore


@pytest.mark.usefixtures("owned_runtime_launches")
def test_scheduled_retirement_refuses_same_native_uuid_after_restart(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    expire, actions = tmp_path / "expire", tmp_path / "actions"
    original = subprocess.Popen
    launcher = f"""
import subprocess,time
from pathlib import Path
import jeff.bootstrap.runtime as runtime
expire,actions=Path({str(expire)!r}),Path({str(actions)!r})
original_store,original_popen=runtime.SQLiteStorage,subprocess.Popen
def store(*args,**kwargs):
    assert kwargs['scheduled_retention'] is True
    kwargs['retention_clock']=lambda:(
        int(expire.read_text()) if expire.exists() else time.time_ns()//1000000
    )
    kwargs['retention_monotonic']=lambda:160.0 if expire.exists() else 100.0
    return original_store(*args,**kwargs)
runtime.SQLiteStorage=store  # Inject at composition; retain the storage module's class.
def popen(command,**kwargs):
    # Count only real answer-adapter status invocations, not legitimate scope rev-parse.
    if ('-C' in command and command[command.index('-C')+1]=={str(git_repo)!r}
        and 'status' in command):
        with actions.open('a') as out:out.write('status\\n')
    return original_popen(command,**kwargs)
subprocess.Popen=popen
raise SystemExit(runtime.main())
"""

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if "jeff.bootstrap.runtime" in command:
            return original([*command[:3], "-c", launcher, *command[5:]], **kwargs)
        return original(command, **kwargs)

    needs_stop, stopped = False, False

    def start() -> ControlSuccess:
        nonlocal needs_stop, stopped
        needs_stop, stopped = True, False
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            result = control.start()
        assert isinstance(result, ControlSuccess) and result.data.application_ready, result
        return result

    def stop() -> None:
        nonlocal needs_stop, stopped
        needs_stop = False  # An ambiguous stop is never retried by finally.
        result = control.stop()
        assert isinstance(result, ControlSuccess), result
        assert not layout.socket.exists()
        owner = RuntimeStateStore(layout).acquire(create=False)
        assert owner is not None
        owner.close()
        stopped = True

    def cli(*arguments: str, expected: int = 0) -> dict[str, Any]:
        env = dict(os.environ)
        env.update(JEFF_DATA_DIR=str(layout.data_dir), JEFF_RUNTIME_DIR=str(layout.runtime_dir))
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "jeff", *arguments, "--json"],
            cwd=git_repo,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == expected, "Owned CLI failed; raw output suppressed"
        data: dict[str, Any] = json.loads(result.stdout)
        return data

    try:
        initial = start()
        registered = cli("init")
        project_id = registered["data"]["project_id"]
        saved = {
            name: (git_repo / name).read_bytes()
            for name in ("jeff.local.yaml", ".gitignore", ".git/HEAD")
        }
        call = ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        with client.subscribe(SubscriptionQuery(directory=git_repo)) as stream:
            reply = client.execute(call)
            assert reply.result.ok and reply.accepted is True
            assert reply.receipt is not None and reply.receipt.finished_at_ms is not None
            assert reply.result.data.project_id == project_id
            observed = []
            for _ in range(10):
                frame = stream.receive(wait_seconds=3)
                assert frame is not None and frame.kind != "closed"
                if frame.event is not None:
                    assert frame.event.request_id == call.request_id
                    observed.append(frame.event.kind)
                    if frame.event.kind == "request.succeeded":
                        break
            assert observed == [
                "request.accepted",
                "request.running",
                "git.finished",
                "git.finished",
                "request.succeeded",
            ]
            old_stream_cursor = stream.last_received_cursor
            assert old_stream_cursor is not None
            debug = cli("debug", "last", "--request-id", str(call.request_id))
            assert debug["data"]["receipt"]["request_id"] == str(call.request_id)
            history = cli("history", "--limit", "1")
            assert history["data"]["items"][0]["request_id"] == str(call.request_id)
            old_history_cursor = history["data"]["next_cursor"]
            assert old_history_cursor is not None
            before = client.inspect_evidence(HistoryQuery(directory=git_repo))
            assert before.history is not None
            expire.write_text(str(reply.receipt.finished_at_ms + 31 * 86400000))
            try:
                closed = stream.receive(wait_seconds=3)
            except ControlFault as error:
                assert error.code == "activity_stream_closed"
            else:
                assert closed is not None and closed.kind == "closed"
                assert closed.error is not None
                assert closed.error.error.code == "activity_invalid_cursor"
        deadline = time.monotonic() + 3
        while True:
            expired = client.inspect_evidence(HistoryQuery(directory=git_repo))
            assert expired.history is not None
            if expired.history.retention_generation > before.history.retention_generation:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert not expired.history.items
        assert expired.history.database_epoch == before.history.database_epoch
        assert expired.history.high_water == before.history.high_water
        assert expired.history.replay_floor == expired.history.high_water
        with pytest.raises(ActivityFault) as stale:
            client.subscribe(SubscriptionQuery(directory=git_repo, cursor=old_stream_cursor))
        assert stale.value.code == "activity_invalid_cursor"
        assert cli("history", "--cursor", old_history_cursor, expected=1)["error"]["code"] == (
            "activity_invalid_cursor"
        )
        assert (
            cli("debug", "last", "--request-id", str(call.request_id), expected=1)["error"]["code"]
            == "receipt_unavailable"
        )
        assert cli("history")["data"]["items"] == []
        action_count = actions.read_bytes()
        assert action_count == b"status\n"
        refused = client.execute(call)
        assert not refused.result.ok and refused.result.error.code == "request_retired"
        assert refused.accepted is False and refused.receipt is None
        after = client.inspect_evidence(HistoryQuery(directory=git_repo))
        assert after.history == expired.history
        assert actions.read_bytes() == action_count
        fresh = client.execute(
            ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        )
        assert fresh.result.ok and fresh.accepted is True
        assert actions.read_bytes() == b"status\nstatus\n"
        stop()
        restarted = start()
        assert initial.data.identity is not None and restarted.data.identity is not None
        assert restarted.data.identity.instance_id != initial.data.identity.instance_id
        refused_again = client.execute(call)
        assert not refused_again.result.ok
        assert refused_again.result.error.code == "request_retired"
        assert refused_again.accepted is False and refused_again.receipt is None
        assert actions.read_bytes() == b"status\nstatus\n"
        assert cli("config")["data"]["project_id"] == project_id
        restarted_page = client.inspect_evidence(HistoryQuery(directory=git_repo))
        assert restarted_page.history is not None
        assert restarted_page.history.database_epoch == expired.history.database_epoch
        assert restarted_page.history.retention_generation == expired.history.retention_generation
        assert {name: (git_repo / name).read_bytes() for name in saved} == saved
    finally:
        if needs_stop:
            stop()
        if stopped:
            layout.credential.unlink(missing_ok=True)
