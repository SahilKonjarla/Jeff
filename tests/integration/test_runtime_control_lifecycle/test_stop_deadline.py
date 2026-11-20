"""Actual acknowledged stop without process exit retains the real unshortened stop deadline."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore


def test_acknowledged_stop_without_process_exit_is_unconfirmed_at_real_deadline(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("hold").resolve()
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    store.credential(owner)
    code = """
import asyncio,os,sys,time
from pathlib import Path
from uuid import uuid4
from jeff.application.runtime import RuntimeLayout,RuntimeIdentityData
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_ipc import ControlServer
os.umask(0o177)
store=RuntimeStateStore(RuntimeLayout(data_dir=Path(sys.argv[1]),runtime_dir=Path(sys.argv[2]),uid=os.geteuid()))
owner=store.adopt(int(sys.argv[3]))
asyncio.run(ControlServer(owner,store.read_credential(),RuntimeIdentityData(instance_id=uuid4(),pid=os.getpid())).serve())
time.sleep(40)  # socket gone, lifetime lock still held by the actual process
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(layout.data_dir),
            str(layout.runtime_dir),
            str(owner.fd),
        ],
        pass_fds=(owner.fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owner.close()
    try:
        deadline = time.monotonic() + 5
        controller = RuntimeController(layout)
        while not isinstance(controller.status(), ControlSuccess):
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        began = time.monotonic()
        result = controller.stop()
        elapsed = time.monotonic() - began
        assert isinstance(result, Failure) and result.error.code == "runtime_stop_unconfirmed"
        assert 34.5 <= elapsed <= 38
        assert child.poll() is None
        assert not layout.socket.exists()
        assert store.acquire(create=False) is None
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        layout.credential.unlink(missing_ok=True)
        layout.socket.unlink(missing_ok=True)
