"""Original nine explicitly authorized native owner-death actor windows, unchanged."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jeff.application.dispatch import DispatchReply, EvidenceQuery, ProjectCall
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController

from .support_git import _git_owner_window
from .support_queued import _QUEUED_BOOT, _normal_stop_under_load
from .support_stream import _healthy_stream_window

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


@pytest.mark.parametrize(
    "window",
    [
        "queued",
        "running",
        "terminal_duplicate",
        "stop_drain",
        "healthy_stream",
        "backpressured_stream",
        "retirement_before_commit",
        "retirement_after_commit",
        "git_in_flight",
    ],
)
def test_active_owner_death(
    tmp_path_factory: pytest.TempPathFactory,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    if os.environ.get("JEFF_TEST_D6_AUTHORITY") != "d6-cap40-exact":
        pytest.skip("Named6D ledger authority required; never an acceptance skip")
    if window in (
        "healthy_stream",
        "backpressured_stream",
        "retirement_before_commit",
        "retirement_after_commit",
    ):
        _healthy_stream_window(tmp_path_factory, git_repo, monkeypatch, window)
        return
    if window == "git_in_flight":
        _git_owner_window(tmp_path_factory, git_repo, monkeypatch)
        return
    assert window in ("queued", "running", "terminal_duplicate", "stop_drain")
    root = tmp_path_factory.mktemp("d").resolve()
    for name in ("home", "tmp", "cache", "config", "data", "gate"):
        (root / name).mkdir(mode=0o700)
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "JEFF_TEST_SQLITE_FIXTURE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }
    original = subprocess.Popen

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["env"] = {
            **kwargs["env"],
            "JEFF_TEST_D6_GATE": str(root / "gate"),
            "JEFF_TEST_D6_WINDOW": window,
        }
        return original([*command[:3], "-c", _QUEUED_BOOT, *command[5:]], **kwargs)

    control = RuntimeController(layout)
    client = RuntimeProjectClient(layout)
    calls = [
        ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        for _ in range(7 if window in ("queued", "stop_drain") else 4 if window == "running" else 1)
    ]
    outcomes: dict[str, DispatchReply | Exception] = {}
    callers: list[threading.Thread] = []
    stopper: threading.Thread | None = None
    stop_outcomes: list[object] = []

    def request_stop() -> None:
        try:
            stop_outcomes.append(control.stop())
        except Exception as error:
            stop_outcomes.append(error)

    def invoke(call: ProjectCall) -> None:
        try:
            outcomes[str(call.request_id)] = client.execute(call)
        except Exception as error:
            outcomes[str(call.request_id)] = error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None
        captured = control._child
        assert captured is not None and captured.poll() is None
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        credential = hashlib.sha256(layout.credential.read_bytes()).hexdigest()
        anchor = layout.lock.stat().st_ino
        (root / "gate/arm").touch(mode=0o600)
        for call in calls:
            thread = threading.Thread(target=invoke, args=(call,))
            callers.append(thread)
            thread.start()
        until = time.monotonic() + 10
        while not (root / "gate/ready").exists():
            assert captured.poll() is None and time.monotonic() < until
            time.sleep(0.01)
        barrier = json.loads((root / "gate/ready").read_text())
        claimed = set(barrier["claimed"])
        expected_count = 4 if window in ("queued", "running", "stop_drain") else 1
        state = (
            "accepted"
            if window in ("queued", "stop_drain")
            else "running"
            if window == "running"
            else "succeeded"
        )
        assert len(claimed) == expected_count
        assert barrier["admitted"] == len(calls) and barrier["helpers_closed"]
        assert barrier["states"] == [state] * expected_count
        if window in ("queued", "stop_drain"):
            assert claimed < {str(call.request_id) for call in calls}
        else:
            assert claimed == {str(call.request_id) for call in calls}
        concurrent_duplicate: DispatchReply | None = None
        if window == "terminal_duplicate":
            assert all(thread.is_alive() for thread in callers)
            concurrent_duplicate = client.execute(calls[0])
            assert not concurrent_duplicate.result.ok
            assert concurrent_duplicate.result.error.code == "duplicate_request"
            assert concurrent_duplicate.receipt is not None
            assert concurrent_duplicate.receipt.state == "succeeded"
            barrier["duplicate_received_terminal_fact"] = True
        if window == "stop_drain":
            stopper = threading.Thread(target=request_stop)
            stopper.start()
            until = time.monotonic() + 5
            while not (root / "gate/draining").exists():
                assert captured.poll() is None and time.monotonic() < until
                time.sleep(0.01)
            assert json.loads((root / "gate/draining").read_text()) == {"admission_sealed": True}
            barrier["actual_drain_started"] = True
        (root / "actor-observation.json").write_text(json.dumps({"window": window, **barrier}))
        if os.environ.get("JEFF_TEST_D6_PREFLIGHT") == "1":
            (root / "gate/arm").unlink()
            for thread in callers:
                thread.join(15)
                assert not thread.is_alive()
            assert len(outcomes) == len(calls)
            if window == "stop_drain":
                assert stopper is not None
                stopper.join(15)
                assert not stopper.is_alive() and len(stop_outcomes) == 1
                assert (
                    isinstance(stop_outcomes[0], ControlSuccess) and captured.wait(timeout=5) == 0
                )
                assert (
                    sum(
                        isinstance(reply, DispatchReply) and reply.result.ok
                        for reply in outcomes.values()
                    )
                    == 4
                )
            else:
                assert all(
                    isinstance(reply, DispatchReply) and reply.result.ok
                    for reply in outcomes.values()
                )
            return  # Explicit barrier-only preflight, excluded from acceptance accounting.
        log = root / "crash-execution.json"
        with log.open("x") as out:
            json.dump(
                {
                    "role": "daemon",
                    "signal": "SIGKILL",
                    "window": window,
                    "execution": os.environ["JEFF_TEST_D6_EXECUTION"],
                    "captured_pid": captured.pid,
                },
                out,
            )
        captured.kill()
        assert captured.wait(timeout=5) == -9
        crash_fact = json.loads(log.read_text())
        crash_fact["native_exit_code"] = -9
        log.write_text(json.dumps(crash_fact))
        if stopper is not None:
            stopper.join(10)
            assert not stopper.is_alive() and len(stop_outcomes) == 1
            assert not isinstance(stop_outcomes[0], Exception)
        for thread in callers:
            thread.join(10)
            assert not thread.is_alive()
        assert len(outcomes) == len(calls)
        for reply in outcomes.values():
            assert isinstance(reply, DispatchReply)
            assert not reply.result.ok and reply.result.error.code == "request_outcome_unknown"
            assert reply.accepted is None and reply.identity == started.data.identity
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in layout.data_dir.iterdir()
            if p.is_file()
        }
        profile = root / "doctor.sb"
        profile.write_text(
            "(version 1)\n(allow default)\n(deny file-write*)\n"
            '(deny network*)\n(allow file-write* (literal "/dev/null"))\n'
        )
        doctor = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-f",
                str(profile),
                sys.executable,
                "-I",
                "-B",
                "-m",
                "jeff",
                "doctor",
                "--json",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert (
            doctor.returncode == 1 and json.loads(doctor.stdout)["data"]["summary"] == "unavailable"
        )
        assert {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in layout.data_dir.iterdir()
            if p.is_file()
        } == before
        (root / "gate/arm").unlink()
        restarted = control.start()
        assert isinstance(restarted, ControlSuccess) and restarted.data.identity is not None
        assert restarted.data.identity.instance_id != started.data.identity.instance_id
        for call in calls:
            trace = client.inspect_evidence(
                EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
            )
            assert trace.error is None
            if str(call.request_id) in claimed:
                assert trace.receipt is not None and trace.trace is not None
                expected_state = "succeeded" if window == "terminal_duplicate" else "unknown"
                assert trace.receipt.state == expected_state
                assert trace.receipt.outcome_code == (
                    "ok" if expected_state == "succeeded" else "interrupted"
                )
                if concurrent_duplicate is not None:
                    assert concurrent_duplicate.receipt == trace.receipt
                duplicate = client.execute(call)
                assert (
                    not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
                )
                assert duplicate.receipt == trace.receipt
            else:
                assert trace.receipt is None and trace.trace is None
                assert trace.missing_is_not_nonexecution_proof  # Never resend these IDs.
        assert hashlib.sha256(layout.credential.read_bytes()).hexdigest() == credential
        assert layout.lock.stat().st_ino == anchor
        assert client.execute(
            ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        ).result.ok
        if window == "stop_drain":
            assert isinstance(control.stop(), ControlSuccess)
            _normal_stop_under_load(layout, git_repo, root / "normal", monkeypatch)
    finally:
        (root / "gate/arm").unlink(missing_ok=True)
        for thread in callers:
            thread.join(15)
            assert not thread.is_alive()
        if stopper is not None:
            stopper.join(15)
            assert not stopper.is_alive()
        assert isinstance(control.stop(), ControlSuccess)
