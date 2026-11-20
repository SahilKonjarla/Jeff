"""Bounded stop preparation and fail-closed endpoint/owner/spawn refusals without children."""

from __future__ import annotations

import socket
import subprocess
import time
from typing import Any

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure import runtime_process
from jeff.infrastructure.runtime_ipc import ControlClient
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore


@pytest.mark.parametrize("code", ["runtime_inspection_failed", "unsafe_runtime_path"])
def test_stop_preparation_bounds_transient_retry_and_preserves_unsafe_failure(
    installation: RuntimeLayout, monkeypatch: pytest.MonkeyPatch, code: Any
) -> None:
    now = 0.0
    attempts = 0

    def clock() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        assert 0 < seconds <= 0.02
        now += seconds

    def refused(layout: RuntimeLayout) -> RuntimeStateStore:
        nonlocal attempts
        attempts += 1
        raise ControlFault(code)

    with monkeypatch.context() as patch:
        patch.setattr(runtime_process, "RuntimeStateStore", refused)
        patch.setattr(time, "monotonic", clock)
        patch.setattr(time, "sleep", sleep)
        with pytest.raises(ControlFault) as failed:
            runtime_process._stop_store(installation, 0.05)
    if code == "unsafe_runtime_path":
        assert failed.value.code == code and attempts == 1 and now == 0
    else:
        assert failed.value.code == "runtime_stop_unconfirmed"
        assert attempts == 3 and now == 0.05


def test_expired_stop_preparation_does_not_attempt_metadata(
    installation: RuntimeLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(layout: RuntimeLayout) -> RuntimeStateStore:
        raise AssertionError("Expired preparation must not touch the namespace")

    with monkeypatch.context() as patch:
        patch.setattr(runtime_process, "RuntimeStateStore", forbidden)
        with pytest.raises(ControlFault) as failed:
            runtime_process._stop_store(installation, time.monotonic() - 1)
    assert failed.value.code == "runtime_stop_unconfirmed"


def test_start_refuses_an_unowned_endpoint_without_minting_credentials(
    installation: RuntimeLayout,
) -> None:
    installation.runtime_dir.mkdir(mode=0o700)
    installation.socket.write_bytes(b"synthetic-collision")
    installation.socket.chmod(0o600)
    result = RuntimeController(installation).start()
    assert isinstance(result, Failure)
    assert result.error.code in ("unsafe_runtime_path", "runtime_endpoint_collision")
    assert not installation.credential.exists()
    assert installation.socket.read_bytes() == b"synthetic-collision"


def test_busy_unknown_owner_times_out_without_creating_replacement_authority(
    installation: RuntimeLayout,
) -> None:
    store = RuntimeStateStore(installation)
    owner = store.acquire(create=True)
    assert owner is not None
    try:
        began = time.monotonic()
        result = RuntimeController(installation).start()
        elapsed = time.monotonic() - began
        assert isinstance(result, Failure) and result.error.code == "runtime_start_timeout"
        assert 8.5 <= elapsed < 10.5
        assert not installation.credential.exists() and not installation.socket.exists()
    finally:
        owner.close()


def test_bound_but_not_listening_endpoint_is_unreachable_not_an_inspection_failure(
    installation: RuntimeLayout,
) -> None:
    installation.runtime_dir.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unpublished:
        unpublished.bind(str(installation.socket))
        installation.socket.chmod(0o600)
        with pytest.raises(ControlFault) as caught:
            ControlClient(installation, bytes(32)).request("hello", deadline=time.monotonic() + 1)
    assert caught.value.code == "runtime_unreachable"


def test_stale_socket_is_preserved_and_never_adopted_or_rekeyed(
    installation: RuntimeLayout,
) -> None:
    installation.runtime_dir.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(installation.socket))
        installation.socket.chmod(0o600)
    before = installation.socket.stat().st_ino
    result = RuntimeController(installation).start()
    assert isinstance(result, Failure) and result.error.code == "runtime_endpoint_collision"
    assert installation.socket.stat().st_ino == before
    assert not installation.credential.exists()


def test_spawn_failure_is_safe_and_does_not_replace_existing_private_state(
    installation: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise OSError("SYNTHETIC_PRIVATE_SPAWN_DETAIL")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", denied)
        result = RuntimeController(installation).start()
    assert isinstance(result, Failure) and result.error.code == "runtime_start_failed"
    assert "SYNTHETIC_PRIVATE_SPAWN_DETAIL" not in result.model_dump_json()
    assert not installation.socket.exists()
    assert isinstance(RuntimeController(installation).status(), ControlSuccess)
