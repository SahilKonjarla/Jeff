"""Captured idle-daemon crash recovery only; no hot journals, active Git or user state."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


@pytest.mark.parametrize("side", ["old_client", "old_server"])
def test_verified_v6_compatibility_does_not_invent_endpoint_proof(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    legacy = os.environ.get("JEFF_TEST_V6_PYTHON")
    if legacy is None:
        pytest.skip("Owned retained-v6 interpreter required; mandatory in guarded acceptance")
    root = tmp_path_factory.mktemp("legacy").resolve()
    for name in ("home", "tmp", "config", "cache", "data"):
        (root / name).mkdir(mode=0o700)
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
    }
    original = subprocess.Popen

    def old_server(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        return original([legacy, *command[1:]], **kwargs)

    controller = RuntimeController(layout)
    try:
        with monkeypatch.context() as patch:
            if side == "old_server":
                patch.setattr(subprocess, "Popen", old_server)
            started = controller.start()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None, started
        again = controller.start()
        assert isinstance(again, ControlSuccess) and again.data.identity == started.data.identity
        if side == "old_server":
            assert not layout.endpoint_receipt.exists()  # legacy health is not recovery proof
        else:
            assert layout.endpoint_receipt.exists()
            code = (
                "from jeff.bootstrap.runtime import create_runtime_controller\n"
                "from jeff.application.runtime import ControlSuccess\n"
                "c=create_runtime_controller();a=c.start();assert isinstance(a,ControlSuccess)\n"
                "s=c.stop();assert isinstance(s,ControlSuccess)\n"
            )
            result = subprocess.run(
                [legacy, "-I", "-B", "-c", code], cwd=root, env=env, capture_output=True, timeout=40
            )
            assert result.returncode == 0
            assert not layout.socket.exists() and not layout.endpoint_receipt.exists()
        assert isinstance(controller.stop(), ControlSuccess)
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def state_snapshot(layout: RuntimeLayout) -> dict[str, tuple[int, int, int, int, str | None]]:
    return {
        str(path): (
            path.lstat().st_ino,
            path.lstat().st_mode,
            path.lstat().st_nlink,
            path.lstat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for root in (layout.data_dir, layout.runtime_dir)
        for path in (root, *root.rglob("*"))
    }


@pytest.mark.parametrize("residue", ["socket_pair", "receipt_only"])
def test_captured_idle_crash_doctor_no_write_and_explicit_restart_preserves_history(
    tmp_path_factory: pytest.TempPathFactory,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    residue: str,
) -> None:
    root = tmp_path_factory.mktemp("b").resolve()
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
    script = """import os,time
from pathlib import Path
import jeff.bootstrap.runtime as runtime
root=Path(os.environ['JEFF_TEST_B6_GATE'])
original=runtime.SQLiteStorage
class IdleStorage(original):
 def _maintain_due(self,connection,directory,fd):
  super()._maintain_due(connection,directory,fd)
  if (root/'arm').exists() and self._queue.empty() and not connection.in_transaction:
   (root/'idle').write_text('committed-idle-no-pending-work')
   while (root/'arm').exists() and not self._closing.is_set():time.sleep(.01)
runtime.SQLiteStorage=IdleStorage
raise SystemExit(runtime.main())
"""

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["env"] = {**kwargs["env"], "JEFF_TEST_B6_GATE": str(root / "gate")}
        return original([*command[:3], "-c", script, *command[5:]], **kwargs)

    def cli(*arguments: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "jeff", *arguments, "--json"],
            cwd=git_repo,
            env=env,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    control = RuntimeController(layout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.identity is not None, started
        owned = control._child
        assert owned is not None and owned.poll() is None
        cli("init")
        history = cli("history")["data"]["items"]
        assert history and history[0]["operation"] == "init"
        project = {
            name: (git_repo / name).read_bytes() for name in ("jeff.local.yaml", ".gitignore")
        }
        key_hash = hashlib.sha256(layout.credential.read_bytes()).hexdigest()
        anchor = layout.lock.stat().st_ino
        (root / "gate/arm").touch()
        deadline = time.monotonic() + 5
        while not (root / "gate/idle").exists():
            assert owned.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        assert (root / "gate/idle").read_text() == "committed-idle-no-pending-work"
        assert not any(layout.data_dir.glob("state.sqlite3-*"))
        receipt_before = layout.endpoint_receipt.read_bytes()
        assert layout.socket.exists() and receipt_before
        evidence = root / "crash-execution.json"
        with os.fdopen(os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as log:
            json.dump(
                {
                    "event": "approved_idle_daemon_sigkill",
                    "captured_pid": owned.pid,
                    "sql_idle": True,
                    "pending_work": 0,
                    "residue": residue,
                },
                log,
            )
        owned.kill()  # named, approved idle-daemon SIGKILL; genuine captured Popen only
        assert owned.wait(timeout=5) == -9
        assert layout.endpoint_receipt.read_bytes() == receipt_before and layout.socket.exists()
        assert not any(layout.data_dir.glob("state.sqlite3-*"))
        owner = RuntimeStateStore(layout).acquire(create=False)
        assert owner is not None
        with owner:
            if residue == "receipt_only":
                original_unlink = os.unlink

                def interrupted(path: str, **kwargs: Any) -> None:
                    original_unlink(path, **kwargs)
                    if path == "daemon.sock":
                        raise OSError(errno.EIO, "SYNTHETIC_BETWEEN_UNLINKS")

                with monkeypatch.context() as patch:
                    patch.setattr(os, "unlink", interrupted)
                    with pytest.raises(ControlFault):
                        owner.prepare_endpoint(time.monotonic() + 5)
                assert not layout.socket.exists() and layout.endpoint_receipt.exists()
        before = state_snapshot(layout)
        profile = root / "doctor.sb"
        profile.write_text(
            "(version 1)\n(allow default)\n(deny file-write*)\n(deny network*)\n"
            '(allow file-write* (literal "/dev/null"))\n'
            + "".join(
                "(deny file-read-data (literal " + json.dumps(str(path)) + "))\n"
                for path in (layout.credential, layout.database, layout.settings)
            )
        )
        prefix = ["/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, "-I", "-B"]
        denial = subprocess.run(
            [
                *prefix,
                "-c",
                'import errno,socket,sys\ntry:open(sys.argv[1],"x").close()\n'
                "except OSError as e:assert e.errno in (errno.EACCES,errno.EPERM)\n"
                'else:raise SystemExit("write allowed")\n'
                'try:open(sys.argv[2],"rb").read(1)\n'
                "except OSError as e:assert e.errno in (errno.EACCES,errno.EPERM)\n"
                'else:raise SystemExit("body read allowed")\n'
                'try:\n s=socket.socket();s.bind(("127.0.0.1",0));s.close()\n'
                "except OSError as e:assert e.errno in (errno.EACCES,errno.EPERM)\n"
                'else:raise SystemExit("TCP allowed")',
                str(root / "home/blocked"),
                str(layout.credential),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert denial.returncode == 0
        doctor = subprocess.run(
            [*prefix, "-m", "jeff", "doctor", "--json"],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert doctor.returncode == 1
        report = json.loads(doctor.stdout)
        assert report["data"]["summary"] == "unavailable" and report["data"]["runtime"] is None
        assert state_snapshot(layout) == before
        assert hashlib.sha256(layout.credential.read_bytes()).hexdigest() == key_hash
        assert layout.lock.stat().st_ino == anchor
        (root / "gate/arm").unlink()
        again = control.start()  # explicit SDK start, no diagnostic-triggered recovery
        assert isinstance(again, ControlSuccess) and again.data.identity is not None, again
        assert again.data.identity.instance_id != started.data.identity.instance_id
        assert layout.endpoint_receipt.read_bytes() != receipt_before
        assert cli("history")["data"]["items"] == history
        assert {name: (git_repo / name).read_bytes() for name in project} == project
        assert hashlib.sha256(layout.credential.read_bytes()).hexdigest() == key_hash
        assert layout.lock.stat().st_ino == anchor
        assert isinstance(control.stop(), ControlSuccess)
        assert not layout.socket.exists() and not layout.endpoint_receipt.exists()
    finally:
        (root / "gate/arm").unlink(missing_ok=True)
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
