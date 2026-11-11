"""Named actual6C transaction crashes only; no default state or external child at death."""

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
from uuid import UUID

import pytest

from jeff.application.dispatch import EvidenceQuery, ProjectCall
from jeff.application.projects import Success
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.project_files import FileProjectStore
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


@pytest.mark.parametrize(
    "window", ["hot_before_commit", "retirement_before_commit", "retirement_after_commit"]
)
def test_crash_commit_window(
    tmp_path_factory: pytest.TempPathFactory,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    if os.environ.get("JEFF_TEST_C6_CRASH_AUTHORITY") != "permit-v1-cap18":
        pytest.skip("Named18-slot6C crash authority required; mandatory in guarded acceptance")
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
    script = """import hashlib,json,os,time
from pathlib import Path
import jeff.bootstrap.runtime as runtime
root=Path(os.environ['JEFF_TEST_C6_GATE']);base=runtime.SQLiteStorage
window=os.environ['JEFF_TEST_C6_WINDOW']
class HeldStorage(base):
 def __init__(self,*args,**kwargs):
  kwargs['retention_clock']=lambda:(
   time.time_ns()//1000000+(32*86400000 if (root/'arm').exists() else 0))
  super().__init__(*args,**kwargs)
 def _open(self,*args):
  connection,fd=super()._open(*args)
  connection.execute('PRAGMA cache_size=1')
  return connection,fd
 def _maintain_due(self,connection,directory,fd):
  if (root/'arm').exists() and not getattr(self,'_fired',False):
   self._fired=True;self._gate_index=True
   return self._retire(connection,directory,fd)
  return super()._maintain_due(connection,directory,fd)
 def _begin_write(self,connection,directory,fd):
  super()._begin_write(connection,directory,fd)
  if getattr(self,'_gate_index',False):
   (root/'baseline').write_text(hashlib.sha256(self.owner.store.layout.database.read_bytes()).hexdigest())
 def _hold(self,connection):
  journal=self.owner.store.layout.data_dir/'state.sqlite3-journal'
  header=journal.read_bytes()[:28] if journal.exists() else b''
  changed=hashlib.sha256(self.owner.store.layout.database.read_bytes()).hexdigest()
  facts={'in_transaction':connection.in_transaction,'journal_magic':header[:8].hex(),
   'database_changed':changed!=(root/'baseline').read_text(),
   'permit_present':self.owner.store.layout.recovery_permit.is_file()}
  (root/'reached.tmp').write_text(json.dumps(facts))
  (root/'reached.tmp').replace(root/'reached')
  while (root/'arm').exists() and not self._closing.is_set():time.sleep(.01)
 def _commit_write(self,connection,fd):
  if getattr(self,'_gate_index',False) and window!='retirement_after_commit':
   self._hold(connection)
  return super()._commit_write(connection,fd)
 def _retire(self,connection,directory,fd):
  result=super()._retire(connection,directory,fd)
  if getattr(self,'_gate_index',False) and window=='retirement_after_commit':
   assert result.retired_count>0
   self._hold(connection)
  return result
runtime.SQLiteStorage=HeldStorage
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

    def cli(command: str, cwd: Path) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "jeff", command, "--json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    control = RuntimeController(layout)
    caller: subprocess.Popen[bytes] | None = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None, started
        captured = control._child
        assert captured is not None and captured.poll() is None
        cli("init", git_repo)
        project_a = FileProjectStore().read(git_repo)
        client = RuntimeProjectClient(layout)
        for _ in range(60):
            configured = client.execute(ProjectCall(operation="config", directory=git_repo))
            assert configured.result.ok
        history = cli("history", git_repo)["data"]["items"]
        baseline_page = client.inspect_evidence(
            EvidenceQuery(operation="replay", directory=git_repo)
        )
        assert baseline_page.page is not None
        baseline_generation = baseline_page.page.retention_generation
        key = hashlib.sha256(layout.credential.read_bytes()).hexdigest()
        anchor = layout.lock.stat().st_ino
        (root / "gate/arm").touch()
        deadline = time.monotonic() + 5
        while not (root / "gate/reached").exists():
            assert captured.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        facts = json.loads((root / "gate/reached").read_text())
        committed = window == "retirement_after_commit"
        assert facts == {
            "in_transaction": not committed,
            "journal_magic": "" if committed else "d9d505f920a163d7",
            "database_changed": True,
            "permit_present": not committed,
        }, facts
        with os.fdopen(
            os.open(root / "crash-execution.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as log:
            json.dump(
                {
                    "event": "approved6c_daemon_sigkill",
                    "window": window,
                    "captured_pid": captured.pid,
                    "real_hot_journal": not committed,
                    "commit_completed": committed,
                },
                log,
            )
        captured.kill()
        assert captured.wait(timeout=5) == -9
        assert layout.recovery_permit.exists() is not committed
        assert (layout.data_dir / "state.sqlite3-journal").exists() is not committed
        # Doctor is a genuinely read-only process before any explicit recovery.
        before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in layout.data_dir.iterdir()
            if p.is_file()
        }
        profile = root / "doctor.sb"
        profile.write_text(
            "(version 1)\n(allow default)\n(deny file-write*)\n(deny network*)\n"
            '(allow file-write* (literal "/dev/null"))\n'
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
        assert isinstance(restarted, ControlSuccess), restarted
        assert restarted.data.identity is not None
        assert restarted.data.identity.instance_id != started.data.identity.instance_id
        assert cli("history", git_repo)["data"]["items"] == ([] if committed else history)
        if committed:
            replay = client.execute(
                ProjectCall(
                    operation="init", directory=git_repo, request_id=UUID(history[0]["request_id"])
                )
            )
            assert not replay.result.ok and replay.result.error.code == "request_retired"
        assert hashlib.sha256(layout.credential.read_bytes()).hexdigest() == key
        assert layout.lock.stat().st_ino == anchor
        assert (
            not layout.recovery_permit.exists()
            and not (layout.data_dir / "state.sqlite3-journal").exists()
        )
        assert isinstance(control.stop(), ControlSuccess)
        owner = RuntimeStateStore(layout).acquire(create=False)
        assert owner is not None
        with owner:
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                assert storage.wait_ready(5).state == "ready"
                assert storage.lookup(project_a) == project_a
                assert storage.retention_position() is not None
                position = storage.retention_position()
                assert position is not None
                assert position.retention_generation == baseline_generation + int(committed)
            finally:
                storage.close()
    finally:
        (root / "gate/arm").unlink(missing_ok=True)
        if caller is not None:
            caller.communicate(timeout=10)
        assert isinstance(control.stop(), ControlSuccess)
