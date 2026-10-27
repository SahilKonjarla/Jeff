"""Real owned native doctor; trusted setup, strict read-only Unix-only product CLI."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore


@pytest.fixture
def doctor_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[RuntimeLayout, dict[str, str], Path]]:
    for name in ("home", "tmp", "config", "cache", "data", "foreign"):
        (tmp_path / name).mkdir(mode=0o700)
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }
    negative = subprocess.run(
        ["/usr/bin/git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"],
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert negative.returncode == 128 and b"not a git repository" in negative.stderr
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def tracked(command: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(command, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", tracked)
    controller = RuntimeController(layout)
    try:
        started = controller.start()
        assert isinstance(started, ControlSuccess), started
        assert started.data.identity is not None
        assert any(child.pid == started.data.identity.pid for child in children)
        yield layout, env, tmp_path / "foreign"
    finally:
        stopped = controller.stop()
        forced = False
        for child in children:
            if child.poll() is None:
                forced = True
                child.terminate()  # only the actual captured Popen, never a stored PID/name
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        assert not forced, "Unexpected forced captured-child cleanup"
        assert isinstance(stopped, ControlSuccess), stopped


def readonly_cli(layout: RuntimeLayout, env: dict[str, str], cwd: Path) -> list[str]:
    profile = cwd.parent / "doctor.sb"
    profile.write_text(
        "(version 1)\n(allow default)\n(deny file-write*)\n"
        '(allow file-write* (literal "/dev/null"))\n(deny network*)\n'
        f"(allow network-outbound (literal {json.dumps(str(layout.socket))}))\n"
        f"(deny file-read-data (literal {json.dumps(str(layout.database))}) "
        f"(literal {json.dumps(str(layout.settings))}))\n"
    )
    prefix = ["/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, "-I", "-B"]
    control = subprocess.run(
        [
            *prefix,
            "-c",
            "import errno,socket,sys\n"
            'for path,mode in ((sys.argv[1],"x"),(sys.argv[2],"rb"),(sys.argv[3],"rb")):\n'
            " try:open(path,mode).close()\n"
            " except OSError as e:assert e.errno in (errno.EPERM,errno.EACCES)\n"
            ' else:raise SystemExit("read/write denial control failed")\n'
            "with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:s.connect(sys.argv[4])\n"
            "with socket.socket() as s:\n"
            " s.settimeout(1)\n"
            ' assert s.connect_ex(("127.0.0.1",9)) in (errno.EPERM,errno.EACCES)\n',
            str(cwd / "blocked"),
            str(layout.database),
            str(layout.settings),
            str(layout.socket),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert control.returncode == 0, control.stderr.decode(errors="replace")
    return prefix


def snapshot(layout: RuntimeLayout) -> dict[str, tuple[int, int, int, int, str | None]]:
    return {
        str(path): (
            path.lstat().st_ino,
            stat.S_IMODE(path.lstat().st_mode),
            path.lstat().st_mtime_ns,
            path.lstat().st_nlink,
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and path != layout.credential
            else None,
        )
        for root in (layout.data_dir, layout.runtime_dir)
        for path in (root, *root.rglob("*"))
    }


def test_live_doctor_uses_authenticated_cache_without_local_sql_or_settings_read(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    # Deliberate inactive fixture edit; active startup-loaded retention is still30.
    layout.settings.write_text("schema_version: 1\nhistory:\n  retention_days: 17\n")
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert snapshot(layout) == before
    report = json.loads(result.stdout)["data"]
    assert report["summary"] == "healthy"
    assert result.returncode == 0 and len(result.stdout) <= 8192
    assert report["runtime"]["application_ready"] is True
    assert report["runtime"]["identity"]["pid"] > 0
    assert report["runtime"]["active_retention_days"] == 30
    assert report["runtime"]["settings_source"] == "default_file"
    assert report["configured_retention_days"] is None
    assert report["settings_schema_version"] is None
    assert report["database_schema_version"] == 4
    assert all(check["status"] == "ok" for check in report["checks"])
    assert str(layout.data_dir) not in result.stdout.decode()


def test_live_doctor_text_labels_active_not_configured_retention(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    layout.settings.write_text("synthetic invalid inactive YAML: [\n")
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor"], cwd=cwd, env=env, capture_output=True, timeout=10
    )
    assert result.returncode == 0 and b"Doctor: healthy" in result.stdout
    assert b"Active retention days: 30" in result.stdout
    assert b"Configured retention" not in result.stdout
    assert snapshot(layout) == before


@pytest.mark.parametrize("fault", ["wrong", "missing", "unsafe"])
def test_live_doctor_credential_fault_never_rekeys_or_claims_health(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    fault: str,
) -> None:
    layout, env, cwd = doctor_installation
    # Only synthetic fixture authority; restore it before authenticated normal teardown.
    original = layout.credential.read_bytes()
    saved = layout.credential.with_name("fixture-saved-key")
    if fault == "wrong":
        layout.credential.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    elif fault == "missing":
        layout.credential.rename(saved)
    else:
        layout.credential.chmod(0o640)
    try:
        prefix = readonly_cli(layout, env, cwd)
        before = snapshot(layout)
        result = subprocess.run(
            [*prefix, "-m", "jeff", "doctor", "--json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and payload["data"]["summary"] == "unhealthy"
        assert payload["data"]["runtime"] is None
        assert payload["data"]["configured_retention_days"] is None
        assert snapshot(layout) == before
        assert original.hex() not in result.stdout.decode() + result.stderr.decode()
        if fault == "wrong":
            assert any(
                c["code"] == "runtime_authentication_failed" for c in payload["data"]["checks"]
            )
        elif fault == "missing":
            assert not layout.credential.exists()
        else:
            assert any(c["status"] == "unsafe" for c in payload["data"]["checks"])
    finally:
        if fault == "missing":
            saved.rename(layout.credential)
        else:
            layout.credential.write_bytes(original)
            layout.credential.chmod(0o600)


@pytest.mark.parametrize(
    "fault,code,summary",
    [
        ("revision", "runtime_protocol_incompatible", "unsupported"),
        ("peer", "runtime_peer_unverified", "unhealthy"),
        ("capability", "runtime_capability_unavailable", "unsupported"),
    ],
)
def test_live_doctor_rejects_injected_protocol_peer_and_capability_faults(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    fault: str,
    code: str,
    summary: str,
) -> None:
    layout, env, cwd = doctor_installation
    if fault == "revision":
        hook = """original=ipc.encode_message
def encode(value,maximum):
 if isinstance(value,dict) and 'token' in value:value={**value,'revision':5}
 return original(value,maximum)
ipc.encode_message=encode
"""
    elif fault == "peer":
        hook = """original=ipc.peer_identity
def peer(connection):
 uid,pid=original(connection)
 return uid,pid+1
ipc.peer_identity=peer
"""
    else:
        hook = """import json
original=ipc.decode_message
def decode(payload,model):
 if model is ipc.ControlResponse:
  value=json.loads(payload)
  if value.get('observation'):
   value['observation']['available_operations'].remove('activity.subscribe')
   payload=json.dumps(value).encode()
 return original(payload,model)
ipc.decode_message=decode
"""
    # Explicit auth-frame/native-peer/codec faults, not mocked DoctorResult or success.
    script = (
        "from jeff.infrastructure import runtime_ipc as ipc\n"
        + hook
        + (
            "from jeff.bootstrap.main import main\nimport sys\n"
            "sys.argv=['jeff','doctor','--json']\nmain()"
        )
    )
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-c", script], cwd=cwd, env=env, capture_output=True, timeout=10
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1 and payload["data"]["summary"] == summary
    assert payload["data"]["runtime"] is None
    assert any(c["code"] == code for c in payload["data"]["checks"])
    assert snapshot(layout) == before


def test_live_doctor_bounds_large_active_retention_after_normal_restart(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    controller = RuntimeController(layout)
    assert isinstance(controller.stop(), ControlSuccess)
    layout.settings.write_text(f"schema_version: 1\nhistory:\n  retention_days: {10**60}\n")
    assert isinstance(controller.start(), ControlSuccess)
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0 and len(result.stdout) <= 8192
    assert payload["data"]["runtime"]["active_retention_days"] == "above_report_limit"
    assert payload["data"]["runtime"]["settings_source"] == "existing_file"
    assert payload["data"]["configured_retention_days"] is None
    assert str(10**60) not in result.stdout.decode()
    assert snapshot(layout) == before


@pytest.fixture
def cached_control(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[RuntimeLayout, dict[str, str], Path, str]]:
    """Actual control/SQL composition without a dispatcher, to observe non-ready caches."""
    mode = str(request.param)
    trace_path = tmp_path / "cache-stop-diagnostic.jsonl"
    trace_path.touch(mode=0o600, exist_ok=False)

    def diagnostic(event: str, **facts: object) -> None:
        with trace_path.open("a") as out:
            out.write(
                json.dumps(
                    {
                        "tag": "6a-cache-stop",
                        "event": event,
                        "monotonic_ns": time.monotonic_ns(),
                        **facts,
                    }
                )
                + "\n"
            )

    class DiagnosticController(RuntimeController):
        def status(self) -> ControlSuccess | Failure:
            diagnostic("status_enter")
            result = super().status()
            diagnostic(
                "status_exit",
                ok=result.ok,
                code=result.error.code if isinstance(result, Failure) else None,
                state=result.data.state.value if isinstance(result, ControlSuccess) else None,
                storage=result.data.storage.state if isinstance(result, ControlSuccess) else None,
            )
            return result

    for name in ("home", "tmp", "config", "cache", "data", "foreign"):
        (tmp_path / name).mkdir(mode=0o700)
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    store.credential(owner)  # new synthetic fixture only; never printed/used outside this arena
    ready, release = tmp_path / "control-ready", tmp_path / "release"
    script = r"""import asyncio,os,sqlite3,sys,time
from pathlib import Path
from uuid import uuid4
from jeff.bootstrap.runtime import _selected_layout
from jeff.application.runtime import RuntimeIdentityData
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.infrastructure.runtime_ipc import ControlServer
os.umask(0o177)
lock,mode,ready,release=sys.argv[1:]
ready,release=Path(ready),Path(release)
store=RuntimeStateStore(_selected_layout());owner=store.adopt(int(lock))
identity=RuntimeIdentityData(instance_id=uuid4(),pid=os.getpid())
original=sqlite3.connect
def diagnostic(event,**facts):
 print(__import__('json').dumps({'tag':'6a-cache-stop-child','event':event,
       'monotonic_ns':time.monotonic_ns(),'pid':os.getpid(),**facts}),file=sys.stderr,flush=True)
class Driver(sqlite3.Connection):
 def execute(self,sql,parameters=()):
  if sql.startswith('CREATE TABLE projects'):
   if mode=='failed':raise sqlite3.OperationalError('SYNTHETIC_PRIVATE_FAILURE')
   if mode=='initializing':
    diagnostic('sql_wait_enter')
    ready.with_name('sql-blocked').touch()
    end=time.monotonic()+8
    while not release.exists():
     if time.monotonic()>=end:raise RuntimeError('fixture release missing')
     time.sleep(.01)
    diagnostic('sql_release_observed')
  return super().execute(sql,parameters)
def connect(*args,**kwargs):return original(*args,**kwargs,factory=Driver)
sqlite3.connect=connect
storage=SQLiteStorage(owner,instance_id=identity.instance_id)
async def serve():
 server=ControlServer(owner,store.read_credential(),identity,storage.snapshot,on_stop=storage.pause_retention)
 task=asyncio.create_task(server.serve());storage.start()
 try:
  while not store.layout.socket.exists():await asyncio.sleep(.01)
  if mode=='initializing':
   while not ready.with_name('sql-blocked').exists():await asyncio.sleep(.01)
  else:
   while storage.snapshot().state=='initializing':await asyncio.sleep(.01)
  if mode=='closing':storage.request_close()
  ready.write_text(storage.snapshot().state)
  diagnostic('control_ready',storage=storage.snapshot().state)
  await task
  diagnostic('server_task_completed',storage=storage.snapshot().state)
 finally:
  diagnostic('close_enter',storage=storage.snapshot().state)
  server.request_stop();await task
  storage.request_close()
  while not storage.closed:await asyncio.sleep(.01)
  diagnostic('storage_thread_closed',storage=storage.snapshot().state)
  storage.close(timeout=0);owner.close()
  diagnostic('owner_closed')
asyncio.run(serve())
"""
    child: subprocess.Popen[bytes] | None = None
    child_stderr = os.fdopen(
        os.open(tmp_path / "cache-child.stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    )
    try:
        diagnostic("spawn_enter")
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                script,
                str(owner.fd),
                mode,
                str(ready),
                str(release),
            ],
            pass_fds=(owner.fd,),
            env=env,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=child_stderr,
        )
        diagnostic("spawn_exit", child_pid=child.pid)
        owner.close()  # close only the actual inherited lock description, never LOCK_UN
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert child.poll() is None, "Owned cache fixture exited before readiness"
            assert time.monotonic() < deadline, "Owned cache fixture readiness timeout"
            time.sleep(0.01)
        diagnostic("fixture_yield", child_pid=child.pid, child_poll=child.poll())
        yield layout, env, tmp_path / "foreign", mode
    finally:
        diagnostic("release_enter", child_poll=child.poll() if child is not None else None)
        release.touch()
        diagnostic("release_exit")
        owner.close()
        diagnostic("stop_enter")
        result = DiagnosticController(layout).stop()
        diagnostic(
            "stop_exit",
            ok=result.ok,
            code=result.error.code if isinstance(result, Failure) else None,
            state=result.data.state.value if isinstance(result, ControlSuccess) else None,
            child_poll=child.poll() if child is not None else None,
        )
        forced = (
            child is not None and child.poll() is None and not isinstance(result, ControlSuccess)
        )
        if forced and child is not None:
            diagnostic("forced_terminate", child_pid=child.pid)
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                diagnostic("forced_kill", child_pid=child.pid)
                child.kill()
                child.wait(timeout=5)
        diagnostic(
            "exit_before_assert",
            child_poll=child.poll() if child is not None else None,
            forced=forced,
        )
        child_stderr.close()
        if child is not None:
            assert child.wait(timeout=5) == 0
        assert not forced, "Unexpected cache fixture forced cleanup"
        assert isinstance(result, ControlSuccess), result


@pytest.mark.parametrize(
    "cached_control", ["ready", "initializing", "failed", "closing"], indirect=True
)
def test_doctor_observes_real_nonready_control_and_storage_cache(
    cached_control: tuple[RuntimeLayout, dict[str, str], Path, str],
) -> None:
    layout, env, cwd, mode = cached_control
    prefix = readonly_cli(layout, env, cwd)
    # Initializing actor is paused; failed/closing/ready actors are quiescent.
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    payload = json.loads(result.stdout)
    report = payload["data"]
    assert result.returncode == 1
    assert report["summary"] == ("unhealthy" if mode == "failed" else "unavailable")
    assert report["runtime"]["application_ready"] is False
    assert report["runtime"]["storage_state"] == mode
    assert report["runtime"]["identity"]["pid"] > 0
    assert report["configured_retention_days"] is None
    assert snapshot(layout) == before
    assert b"SYNTHETIC_PRIVATE_FAILURE" not in result.stdout + result.stderr


def test_doctor_does_not_displace_project_history_or_debug(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    git_repo: Path,
) -> None:
    layout, env, cwd = doctor_installation

    def invoke(*args: str) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "jeff", *args],
            cwd=git_repo,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return result

    invoke("init", "--json")
    invoke("ask", "what branch am I on?", "--json")
    before_debug = invoke("debug", "last", "--json").stdout
    before_history = invoke("history", "--json").stdout
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["data"]["summary"] == "healthy"
    assert snapshot(layout) == before
    assert invoke("debug", "last", "--json").stdout == before_debug
    assert invoke("history", "--json").stdout == before_history


@pytest.mark.parametrize("fault", ["silent", "partial"])
def test_owned_unresponsive_peer_uses_shared_deadline_not_false_health(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    fault: str,
) -> None:
    layout, env, cwd = doctor_installation
    assert isinstance(RuntimeController(layout).stop(), ControlSuccess)
    owner = RuntimeStateStore(layout).acquire(create=False)
    assert owner is not None
    stop = threading.Event()
    errors: list[BaseException] = []
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        endpoint = layout.socket.lstat()
        listener.listen(4)
        listener.settimeout(0.1)

        def serve() -> None:
            try:
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with connection:
                        connection.settimeout(1)
                        if not connection.recv(4096):
                            continue  # selected-Unix-socket allow control closes without auth
                        if fault == "partial":
                            connection.sendall(b"\x00\x00\x00\x20{")
                        stop.wait(8)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            prefix = readonly_cli(layout, env, cwd)
            before = snapshot(layout)
            began = time.monotonic()
            result = subprocess.run(
                [*prefix, "-m", "jeff", "doctor", "--json"],
                cwd=cwd,
                env=env,
                capture_output=True,
                timeout=10,
            )
            elapsed = time.monotonic() - began
            payload = json.loads(result.stdout)
            assert result.returncode == 1 and payload["data"]["summary"] == "unavailable"
            assert payload["data"]["runtime"] is None
            assert any(
                c["code"] == "diagnostic_deadline_exceeded" for c in payload["data"]["checks"]
            )
            assert 4 <= elapsed < 7
            assert snapshot(layout) == before
        finally:
            stop.set()
            thread.join(timeout=2)
            assert not thread.is_alive() and not errors
            current = layout.socket.lstat()
            assert (current.st_dev, current.st_ino) == (endpoint.st_dev, endpoint.st_ino)
            layout.socket.unlink()  # fixture removes ONLY the socket it just bound/captured


def test_doctor_discards_live_facts_after_controlled_namespace_replacement(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    prefix = readonly_cli(layout, env, cwd)
    # Actor runs outside the product sandbox; doctor only signals a captured pipe.
    notify_read, notify_write = os.pipe()
    release_read, release_write = os.pipe()
    moved = layout.runtime_dir.with_name("fixture-moved-runtime")
    errors: list[BaseException] = []

    def actor() -> None:
        try:
            assert os.read(notify_read, 1) == b"1"
            layout.runtime_dir.rename(moved)
            layout.runtime_dir.mkdir(mode=0o700)
        except BaseException as error:
            errors.append(error)
        finally:
            os.write(release_write, b"1")

    script = f"""import os
from jeff.infrastructure import runtime_ipc as ipc
original=ipc.ControlConnection.request
def request(self,*args,**kwargs):
 result=original(self,*args,**kwargs)
 os.write({notify_write},b'1');os.read({release_read},1)
 return result
ipc.ControlConnection.request=request
from jeff.bootstrap.main import main
import sys
sys.argv=['jeff','doctor','--json'];main()
"""
    thread = threading.Thread(target=actor)
    thread.start()
    try:
        result = subprocess.run(
            [*prefix, "-c", script],
            cwd=cwd,
            env=env,
            capture_output=True,
            pass_fds=(notify_write, release_read),
            timeout=10,
        )
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and payload["data"]["summary"] == "unavailable"
        assert payload["data"]["runtime"] is None
        assert payload["data"]["database_schema_version"] is None
    finally:
        os.close(notify_write)
        thread.join(timeout=2)
        assert not thread.is_alive() and not errors
        for fd in (notify_read, release_read, release_write):
            os.close(fd)
        if moved.exists():
            layout.runtime_dir.rmdir()  # the actor's new, empty fixture directory only
            moved.rename(layout.runtime_dir)


def test_live_doctor_uses_cache_during_actual_sqlite_write_lock(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    old_mask = os.umask(0o177)
    try:
        with closing(sqlite3.connect(layout.database, timeout=0, isolation_level=None)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            try:
                writer.execute("UPDATE schema_migrations SET name=name WHERE version=4")
                assert (layout.data_dir / "state.sqlite3-journal").exists()
                prefix = readonly_cli(layout, env, cwd)
                before = snapshot(layout)
                began = time.monotonic()
                result = subprocess.run(
                    [*prefix, "-m", "jeff", "doctor", "--json"],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
                assert (
                    result.returncode == 0
                    and json.loads(result.stdout)["data"]["summary"] == "healthy"
                )
                assert time.monotonic() - began < 3
                assert snapshot(layout) == before
            finally:
                writer.execute("ROLLBACK")
    finally:
        os.umask(old_mask)


def test_doctor_admission_exhaustion_is_unavailable_and_slots_recover(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    peers: list[socket.socket] = []
    try:
        for _ in range(16):
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peers.append(peer)
            peer.settimeout(2)
            peer.connect(str(layout.socket))
            peer.sendall(b"\x00")  # real incomplete auth frames retain the bounded slots
        prefix = readonly_cli(layout, env, cwd)
        result = subprocess.run(
            [*prefix, "-m", "jeff", "doctor", "--json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and payload["data"]["summary"] == "unavailable"
        assert payload["data"]["runtime"] is None
    finally:
        for peer in peers:
            peer.close()
    again = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert again.returncode == 0 and json.loads(again.stdout)["data"]["summary"] == "healthy"


def test_doctor_discards_live_facts_when_normal_stop_finishes_during_observation(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    prefix = readonly_cli(layout, env, cwd)
    notify_read, notify_write = os.pipe()
    release_read, release_write = os.pipe()
    outcomes: list[object] = []

    def stop_actor() -> None:
        try:
            assert os.read(notify_read, 1) == b"1"
            outcomes.append(RuntimeController(layout).stop())
        except BaseException as error:
            outcomes.append(error)
        finally:
            os.write(release_write, b"1")

    script = f"""import os
from jeff.infrastructure import runtime_ipc as ipc
original=ipc.ControlConnection.request
def request(self,*args,**kwargs):
 result=original(self,*args,**kwargs)
 os.write({notify_write},b'1');os.read({release_read},1)
 return result
ipc.ControlConnection.request=request
from jeff.bootstrap.main import main
import sys
sys.argv=['jeff','doctor','--json'];main()
"""
    thread = threading.Thread(target=stop_actor)
    thread.start()
    try:
        result = subprocess.run(
            [*prefix, "-c", script],
            cwd=cwd,
            env=env,
            capture_output=True,
            pass_fds=(notify_write, release_read),
            timeout=10,
        )
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and payload["data"]["summary"] == "unavailable"
        assert payload["data"]["runtime"] is None
        assert payload["data"]["database_schema_version"] is None
    finally:
        os.close(notify_write)
        thread.join(timeout=2)
        for fd in (notify_read, release_read, release_write):
            os.close(fd)
        assert not thread.is_alive()
        assert len(outcomes) == 1 and isinstance(outcomes[0], ControlSuccess), outcomes


def test_doctor_reports_real_startup_in_progress_then_healthy(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_layout, env, cwd = doctor_installation
    root = original_layout.data_dir.parent
    layout = RuntimeLayout(data_dir=root / "e", runtime_dir=root / "s", uid=os.geteuid())
    selected_env = {
        **env,
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
    }
    marker, release = root / "starting", root / "continue-start"
    original = subprocess.Popen
    script = f"""import sqlite3,time
from pathlib import Path
marker,release=Path({str(marker)!r}),Path({str(release)!r})
connect=sqlite3.connect
class Driver(sqlite3.Connection):
 def execute(self,sql,parameters=()):
  if sql.startswith('CREATE TABLE projects'):
   marker.touch();end=time.monotonic()+8
   while not release.exists():
    if time.monotonic()>=end:raise RuntimeError('fixture release missing')
    time.sleep(.01)
  return super().execute(sql,parameters)
def wrapped(*args,**kwargs):return connect(*args,**kwargs,factory=Driver)
sqlite3.connect=wrapped
from jeff.bootstrap.runtime import main
raise SystemExit(main())
"""

    def start_at_driver(command: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        if "jeff.bootstrap.runtime" in command and kwargs.get("env", {}).get(
            "JEFF_RUNTIME_DIR"
        ) == str(layout.runtime_dir):
            command = [*command[:3], "-c", script, *command[5:]]
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", start_at_driver)
    controller = RuntimeController(layout)
    outcomes: list[object] = []

    def start_actor() -> None:
        try:
            outcomes.append(controller.start())
        except BaseException as error:
            outcomes.append(error)

    thread = threading.Thread(target=start_actor)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline and thread.is_alive(), outcomes
            time.sleep(0.01)
        prefix = readonly_cli(layout, selected_env, cwd)
        result = subprocess.run(
            [*prefix, "-m", "jeff", "doctor", "--json"],
            cwd=cwd,
            env=selected_env,
            capture_output=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and payload["data"]["summary"] == "unavailable"
        assert payload["data"]["runtime"]["storage_state"] == "initializing"
    finally:
        release.touch()
        thread.join(timeout=12)
        assert not thread.is_alive() and len(outcomes) == 1
        try:
            assert isinstance(outcomes[0], ControlSuccess), outcomes
            healthy = subprocess.run(
                [*prefix, "-m", "jeff", "doctor", "--json"],
                cwd=cwd,
                env=selected_env,
                capture_output=True,
                timeout=10,
            )
            assert (
                healthy.returncode == 0
                and json.loads(healthy.stdout)["data"]["summary"] == "healthy"
            )
        finally:
            assert isinstance(controller.stop(), ControlSuccess)
