"""Original authority-gated committed receipt and registration publication crash windows."""

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

from jeff.application.dispatch import EvidenceQuery, ProjectCall
from jeff.application.projects import Success
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


@pytest.mark.parametrize(
    "window",
    [
        "claim_committed",
        "terminal_committed",
        "init_ignore",
        "init_yaml",
        "init_registry_committed",
    ],
)
def test_crash_commit_window_publication(
    tmp_path_factory: pytest.TempPathFactory,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    if os.environ.get("JEFF_TEST_C6_CRASH_AUTHORITY") != "permit-v1-cap18":
        pytest.skip("Explicit named6C crash authority required")
    root = tmp_path_factory.mktemp("c").resolve()
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
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }
    original = subprocess.Popen
    script = """import json,os,time
from pathlib import Path
import jeff.bootstrap.runtime as runtime
from jeff.infrastructure.project_files import FileProjectStore
root=Path(os.environ['JEFF_TEST_C6_GATE']);window=os.environ['JEFF_TEST_C6_WINDOW']
def hold():
 if not (root/'arm').exists():return
 (root/'reached.tmp').write_text(json.dumps({'window':window}))
 (root/'reached.tmp').replace(root/'reached')
 while (root/'arm').exists():time.sleep(.01)
base=runtime.SQLiteStorage
class Held(base):
 def _receipt_work(self,connection,work,directory,fd):
  result=super()._receipt_work(connection,work,directory,fd)
  matched=(window=='claim_committed' and work.operation=='claim')
  matched=matched or (window=='terminal_committed' and
   work.operation=='finish' and getattr(result,'state',None)=='succeeded')
  if matched and (root/'arm').exists():
   assert not connection.in_transaction
   assert not self.owner.store.layout.recovery_permit.exists()
   hold()
  return result
 def _execute(self,connection,work,directory,fd):
  result=super()._execute(connection,work,directory,fd)
  if window=='init_registry_committed' and work.operation=='index':
   assert not connection.in_transaction
   hold()
  return result
write=FileProjectStore._write
def publish(directory,name,content,**kwargs):
 write(directory,name,content,**kwargs)
 if (window=='init_ignore' and name=='.gitignore') or (
     window=='init_yaml' and name=='jeff.local.yaml'):hold()
FileProjectStore._write=staticmethod(publish)
runtime.SQLiteStorage=Held
raise SystemExit(runtime.main())
"""

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["env"] = {
            **kwargs["env"],
            "JEFF_TEST_C6_GATE": str(root / "gate"),
            "JEFF_TEST_C6_WINDOW": window,
        }
        return original([*command[:3], "-c", script, *command[5:]], **kwargs)

    control = RuntimeController(layout)
    client = RuntimeProjectClient(layout)
    outcome: list[object] = []
    caller: threading.Thread | None = None
    call = ProjectCall(operation="init", directory=git_repo)

    def invoke() -> None:
        try:
            outcome.append(client.execute(call))
        except Exception as error:
            outcome.append(error)

    def files() -> dict[str, bytes | None]:
        return {
            name: (git_repo / name).read_bytes() if (git_repo / name).exists() else None
            for name in (".gitignore", "jeff.local.yaml")
        }

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None
        captured = control._child
        assert captured is not None and captured.poll() is None
        if not window.startswith("init_"):
            assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        key = hashlib.sha256(layout.credential.read_bytes()).hexdigest()
        anchor = layout.lock.stat().st_ino
        (root / "gate/arm").touch()
        caller = threading.Thread(target=invoke)
        caller.start()
        deadline = time.monotonic() + 5
        while not (root / "gate/reached").exists():
            assert captured.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        assert json.loads((root / "gate/reached").read_text()) == {"window": window}
        published = files()
        with (root / "crash-execution.json").open("x") as log:
            json.dump(
                {
                    "event": "approved6c_daemon_sigkill",
                    "window": window,
                    "captured_pid": captured.pid,
                    "request_id": str(call.request_id),
                },
                log,
            )
        captured.kill()
        assert captured.wait(timeout=5) == -9
        caller.join(10)
        assert not caller.is_alive() and len(outcome) == 1
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
        assert doctor.returncode == 1
        assert json.loads(doctor.stdout)["data"]["summary"] == "unavailable"
        assert {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in layout.data_dir.iterdir()
            if p.is_file()
        } == before
        (root / "gate/arm").unlink()
        restarted = control.start()
        assert isinstance(restarted, ControlSuccess) and restarted.data.identity is not None
        assert restarted.data.identity.instance_id != started.data.identity.instance_id
        assert files() == published
        trace = client.inspect_evidence(
            EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
        )
        assert trace.receipt is not None and trace.trace is not None
        expected = "succeeded" if window == "terminal_committed" else "unknown"
        assert trace.receipt.state == expected
        assert trace.receipt.outcome_code == ("ok" if expected == "succeeded" else "interrupted")
        assert trace.trace.events[-1].kind == (
            "request.succeeded" if expected == "succeeded" else "request.interrupted"
        )
        duplicate = client.execute(call)
        assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
        assert duplicate.receipt == trace.receipt and files() == published
        if window.startswith("init_"):
            # Existing public storage lookup distinguishes file publication from SQL COMMIT.
            assert isinstance(control.stop(), ControlSuccess)
            owner = RuntimeStateStore(layout).acquire(create=False)
            assert owner is not None
            with owner:
                store = SQLiteStorage(owner)
                store.start()
                try:
                    assert store.wait_ready(5).state == "ready"
                    assert store.root_claimed(git_repo) is (window == "init_registry_committed")
                finally:
                    store.close()
            assert isinstance(control.start(), ControlSuccess)
            repaired = client.execute(ProjectCall(operation="init", directory=git_repo))
            assert isinstance(repaired.result, Success)
            if window != "init_ignore":
                assert files() == published
                assert repaired.result.data.created is False
            else:
                assert files()[".gitignore"] == published[".gitignore"]
                assert repaired.result.data.created is True
            again = client.inspect_evidence(
                EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
            )
            assert again.receipt == trace.receipt  # explicit new init never rewrites old truth
        assert hashlib.sha256(layout.credential.read_bytes()).hexdigest() == key
        assert layout.lock.stat().st_ino == anchor
    finally:
        (root / "gate/arm").unlink(missing_ok=True)
        if caller is not None:
            caller.join(10)
            assert not caller.is_alive()
        assert isinstance(control.stop(), ControlSuccess)
