"""Original function-scoped captured runtime and non-ready cache actors, unchanged."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
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
        "JEFF_TEST_SQLITE_FIXTURE": "1",
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
        "JEFF_TEST_SQLITE_FIXTURE": "1",
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
