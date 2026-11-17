"""Original per-test owned launch tracking and isolated installation cleanup."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController


@pytest.fixture
def owned_launches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def tracked(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(command, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", tracked)
    yield
    forced = False
    for child in children:
        if child.poll() is None:
            forced = True
            child.terminate()  # actual captured Popen ownership, never a stored/guessed PID
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    assert not forced, "A fixture required forced owned-child cleanup"


@pytest.fixture
def installation(
    tmp_path_factory: pytest.TempPathFactory, owned_launches: None
) -> Iterator[RuntimeLayout]:
    root = tmp_path_factory.mktemp("inst").resolve()
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    yield layout
    result = RuntimeController(layout).stop()
    # Unsafe/collision fixtures may intentionally refuse stop; captured handles still reap.
    if isinstance(result, Failure):
        assert result.error.code in (
            "unsafe_runtime_path",
            "runtime_endpoint_collision",
            "runtime_credentials_invalid",
        )
    layout.credential.unlink(missing_ok=True)
    layout.socket.unlink(missing_ok=True)
