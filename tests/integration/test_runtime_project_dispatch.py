"""Bounded project work through declared interfaces, never a private queue probe."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.dispatch import (
    DispatchReply,
    ProjectCall,
    ProjectDispatcher,
    ProjectRequest,
    ReceiptQuery,
)
from jeff.application.history import GitObserver
from jeff.application.projects import ProjectService
from jeff.application.questions import QuestionService
from jeff.application.runtime import (
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
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead


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
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    marker = tmp_path / "git-status-count"
    original = subprocess.Popen
    # System subprocess fault in the actual child, never a mocked dispatcher result.
    launcher = f"""
import runpy,subprocess,sys
from pathlib import Path
original=subprocess.Popen
marker=Path({str(marker)!r})
def launch(command,*args,**kwargs):
    if command[0]=='/usr/bin/git' and 'status' in command:
        with marker.open('a') as out:out.write('1\\n')
        code=(\"import sys,time;sys.stdout.buffer.write(b'# branch.oid (initial)'+bytes([0])+\"
              \"b'# branch.head main'+bytes([0]));sys.stdout.flush();time.sleep(3);\"
              \"open(sys.argv[1],'a').write('1'+chr(10))\")
        return original([sys.executable,'-I','-B','-c',code,str(marker)+'.done'],*args,**kwargs)
    return original(command,*args,**kwargs)
subprocess.Popen=launch
runpy.run_module('jeff.bootstrap.runtime',run_name='__main__')
"""

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
                    remaining_ms=300 if mode == "timeout" else 30000,
                )
                connection.connection.sendall(encode_message(wire.model_dump(mode="json"), 65536))
            until = time.monotonic() + 3
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
        duplicate = client.execute(call)
        assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
        if mode != "saturated":
            assert marker.read_text() == "1\n"
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
