"""Bounded project work through declared interfaces, never a private queue probe."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.dispatch import (
    DispatchReply,
    EvidenceQuery,
    ProjectCall,
    ProjectDispatcher,
    ProjectRequest,
    ReceiptQuery,
)
from jeff.application.history import GitObserver
from jeff.application.projects import ProjectService
from jeff.application.questions import QuestionService
from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    ControlFault,
    ControlSuccess,
    RuntimeIdentityData,
    RuntimeLayout,
)
from jeff.domain.repository import BranchState
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_ipc import ControlClient, RuntimeProjectClient, encode_message
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GIT_TIMEOUT, GitProjectDiscovery, GitRepositoryRead


def test_four_active_sixteen_pending_and_actual_drain(git_repo: Path, tmp_path: Path) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    entered = threading.Condition()
    active = 0
    release = threading.Event()

    class HeldGit(GitRepositoryRead):
        def read_branch(
            self, root: Path, *, deadline: float | None = None, observer: GitObserver | None = None
        ) -> BranchState:
            nonlocal active
            with entered:
                active += 1
                entered.notify_all()
            assert release.wait(10)
            return super().read_branch(root, deadline=deadline, observer=observer)

    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner, instance_id=identity.instance_id)
        storage.start()
        assert storage.wait_ready(5).state == "ready"
        workers = RuntimeDispatcher(
            ProjectDispatcher(projects, QuestionService(projects, HeldGit()), storage, identity)
        )
        workers.start()
        try:
            first = [
                workers.submit(
                    ProjectCall(
                        operation="ask", directory=git_repo, question="what branch am I on?"
                    ),
                    time.monotonic() + 20,
                )
                for _ in range(4)
            ]
            with entered:
                assert entered.wait_for(lambda: active == 4, timeout=5)
            pending = [
                workers.submit(
                    ProjectCall(
                        operation="ask", directory=git_repo, question="what branch am I on?"
                    ),
                    time.monotonic() + 20,
                )
                for _ in range(16)
            ]
            with pytest.raises(ControlFault) as busy:
                workers.submit(
                    ProjectCall(
                        operation="ask", directory=git_repo, question="what branch am I on?"
                    ),
                    time.monotonic() + 20,
                )
            assert busy.value.code == "runtime_busy"
            with pytest.raises(ControlFault):
                workers.close(timeout=0)
            assert not workers.closed
            release.set()
            completed = [f.result(timeout=5) for f in first]
            refused = [f.result(timeout=5) for f in pending]
            assert all(isinstance(r, DispatchReply) and r.result.ok for r in completed)
            assert all(isinstance(r, DispatchReply) and not r.accepted for r in refused)
        finally:
            release.set()
            workers.close()
            storage.close()
        assert workers.closed


@pytest.mark.usefixtures("owned_runtime_launches")
@pytest.mark.parametrize("mode", ["disconnect", "timeout", "saturated"])
def test_native_slow_git_delivery_failure_keeps_control_and_receipt_honest(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    git_helper_script: Callable[[str, str], str],
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    marker = tmp_path / "git-status-count"
    original = subprocess.Popen
    # Give admission/discovery its normal budget; induce timeout only after Git starts.
    slow_seconds = GIT_TIMEOUT + 1 if mode == "timeout" else 3
    # System subprocess fault in the actual child, never a mocked dispatcher result.
    helper_hook = f"""
import atexit,json,os,subprocess,sys,time
from pathlib import Path
original=subprocess.Popen
children=[];killed=[];original_kill=original.kill
deadline_ns=int(sys.argv[sys.argv.index('--deadline-ns')+1])
marker=Path({str(marker)!r})
def kill(process):
    if process in children and process.poll() is None:killed.append((process,time.monotonic_ns()))
    return original_kill(process)
original.kill=kill
def record():
    if not killed:return
    fd=os.open(marker.with_name('slow-probe-signals.jsonl'),os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    try:
        for process,killed_ns in killed:
            row={{'role':'python_slow_fixture','signal_call':'SIGKILL',
                  'captured_pid':process.pid,'native_exit_code':process.poll(),
                  'deadline_ns':deadline_ns,'killed_ns':killed_ns}}
            os.write(fd,(json.dumps(row)+'\\n').encode())
    finally:os.close(fd)
atexit.register(record)
def launch(command,*args,**kwargs):
    if command[0]=='/usr/bin/git' and 'status' in command:
        with marker.open('a') as out:out.write('1\\n')
        code=(\"import sys,time;sys.stdout.buffer.write(b'# branch.oid (initial)'+bytes([0])+\"
              \"b'# branch.head main'+bytes([0]));sys.stdout.flush();time.sleep({slow_seconds});\"
              \"open(sys.argv[1],'a').write('1'+chr(10))\")
        child=original([sys.executable,'-I','-B','-c',code,str(marker)+'.done'],*args,**kwargs)
        children.append(child)
        return child
    return original(command,*args,**kwargs)
subprocess.Popen=launch
"""
    launcher = git_helper_script(
        helper_hook, "import runpy;runpy.run_module('jeff.bootstrap.runtime',run_name='__main__')"
    )

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if "jeff.bootstrap.runtime" in command:
            return original([*command[:3], "-c", launcher, *command[5:]], **kwargs)
        return original(command, **kwargs)

    controller = RuntimeController(layout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = controller.start()
        assert isinstance(started, ControlSuccess), started
        client = RuntimeProjectClient(layout)
        call = ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        token = RuntimeStateStore(layout).read_credential()
        with ExitStack() as connections:
            for number in range(15 if mode == "saturated" else 1):
                connection = connections.enter_context(
                    ControlClient(layout, token).connect(time.monotonic() + 5)
                )
                wire = ProjectRequest(
                    call=call if number == 0 else call.model_copy(update={"request_id": uuid4()}),
                    expected_instance_id=connection.identity.instance_id,
                    remaining_ms=30000,
                )
                connection.connection.sendall(encode_message(wire.model_dump(mode="json"), 65536))
            until = time.monotonic() + (
                DEFAULT_RUNTIME_LIMITS.request_seconds if mode == "timeout" else 3
            )
            while not marker.exists():
                assert time.monotonic() < until
                time.sleep(0.01)
            began = time.monotonic()
            status = controller.status()
            assert isinstance(status, ControlSuccess) and time.monotonic() - began < 1
            if mode == "saturated":
                assert isinstance(controller.stop(), ControlSuccess)
                assert Path(str(marker) + ".done").read_text() == marker.read_text()
        if mode == "saturated":
            assert isinstance(controller.start(), ControlSuccess)
        until = time.monotonic() + 6
        while True:
            found = client.inspect_receipt(
                ReceiptQuery(directory=git_repo, request_id=call.request_id)
            )
            if found.receipt is not None and found.receipt.state in (
                "succeeded",
                "failed",
                "unknown",
            ):
                break
            assert time.monotonic() < until
            time.sleep(0.02)
        if mode in ("disconnect", "saturated"):
            assert found.receipt.state == "succeeded"
        else:
            assert found.receipt.state != "succeeded"
            evidence = client.inspect_evidence(
                EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
            )
            assert evidence.error is None and evidence.trace is not None
            assert any(
                event.git is not None
                and event.git.process_started
                and event.git.code in ("git_timeout", "git_failed")
                for event in evidence.trace.events
            )
            signals = [
                json.loads(line)
                for line in (tmp_path / "slow-probe-signals.jsonl").read_text().splitlines()
            ]
            assert len(signals) == 1
            assert signals[0]["signal_call"] == "SIGKILL" and signals[0]["native_exit_code"] == -9
            assert signals[0]["killed_ns"] >= signals[0]["deadline_ns"]
        duplicate = client.execute(call)
        assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
        if mode != "saturated":
            assert marker.read_text() == "1\n"
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
