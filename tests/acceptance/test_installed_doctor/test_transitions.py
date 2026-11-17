"""Controlled namespace replacement, completed normal stop and real startup observations."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController

from .support_readonly import readonly_cli


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
