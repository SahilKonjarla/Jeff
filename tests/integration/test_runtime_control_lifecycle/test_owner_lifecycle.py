"""Actual owned runtime publication, churn-aware stop, repeat/concurrent starts and restart."""

from __future__ import annotations

import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import ControlObservation, ControlSuccess, RuntimeLayout
from jeff.infrastructure import runtime_paths
from jeff.infrastructure.runtime_ipc import ControlConnection
from jeff.infrastructure.runtime_process import RuntimeController


def test_real_runtime_publishes_endpoint_proof_before_ready_and_removes_it_on_stop(
    installation: RuntimeLayout,
) -> None:
    controller = RuntimeController(installation)
    try:
        result = controller.start()
        assert isinstance(result, ControlSuccess) and result.data.application_ready, result
        receipt = installation.runtime_dir / "endpoint.json"
        assert receipt.is_file() and 0 < receipt.stat().st_size <= 2048
        assert receipt.stat().st_mode & 0o7777 == 0o600 and receipt.stat().st_nlink == 1
        assert isinstance(controller.stop(), ControlSuccess)
        assert not receipt.exists() and not installation.socket.exists()
        assert installation.lock.exists() and installation.credential.exists()
    finally:
        assert isinstance(controller.stop(), ControlSuccess)


def test_native_publication_io_failure_never_claims_ready(
    installation: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = subprocess.Popen
    script = (
        "import os,errno\n"
        "original=os.link\n"
        "def link(src,dst,**kwargs):\n"
        " if dst=='endpoint.json':raise OSError(errno.EIO,'SYNTHETIC_PUBLICATION_IO')\n"
        " return original(src,dst,**kwargs)\n"
        "os.link=link\n"
        "from jeff.bootstrap.runtime import main\n"
        "raise SystemExit(main())\n"
    )

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        return original([*command[:3], "-c", script, *command[5:]], **kwargs)

    controller = RuntimeController(installation)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            result = controller.start()
        assert isinstance(result, Failure), result
        assert controller._child is not None and controller._child.poll() is not None
        assert not installation.socket.exists() and not installation.endpoint_receipt.exists()
        assert isinstance(controller.stop(), ControlSuccess)
    finally:
        assert isinstance(controller.stop(), ControlSuccess)


def test_stop_retries_real_directory_entry_churn_without_resending_stop(
    installation: RuntimeLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = RuntimeController(installation)
    assert isinstance(controller.start(), ControlSuccess)
    original_status = RuntimeController.status
    original_chain = runtime_paths._chain_stable
    original_request = ControlConnection.request
    after_status = False
    injected = False
    rejected = False
    stop_requests = 0

    def status(self: RuntimeController) -> ControlSuccess | Failure:
        nonlocal after_status
        result = original_status(self)
        after_status = True
        return result

    def chain(
        entries: list[tuple[int, str, os.stat_result, int]], *, refresh_allowed: bool
    ) -> bool:
        nonlocal injected, rejected
        if after_status and not injected and entries[-1][1] == installation.data_dir.name:
            parent, name, before, pinned = entries[-1]
            directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                actual = os.fstat(directory)
                assert (actual.st_dev, actual.st_ino) == (before.st_dev, before.st_ino)
                marker = os.open(
                    "metadata-churn",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                os.close(marker)
                injected = True
            finally:
                os.close(directory)
            original_fstat = os.fstat

            def continued(fd: int) -> os.stat_result:
                if fd == pinned:
                    # A second actual change makes the fresh path/FD pair inconsistent.
                    marker = os.open(
                        "metadata-churn-continued",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=pinned,
                    )
                    os.close(marker)
                return original_fstat(fd)

            with monkeypatch.context() as sample:
                sample.setattr(os, "fstat", continued)
                result = original_chain(entries, refresh_allowed=refresh_allowed)
            rejected = not result
            return result
        return original_chain(entries, refresh_allowed=refresh_allowed)

    def request(
        self: ControlConnection, operation: Any, expected_instance_id: UUID | None = None
    ) -> ControlObservation:
        nonlocal stop_requests
        if operation == "stop":
            stop_requests += 1
        return original_request(self, operation, expected_instance_id)

    with monkeypatch.context() as patch:
        patch.setattr(RuntimeController, "status", status)
        patch.setattr(runtime_paths, "_chain_stable", chain)
        patch.setattr(ControlConnection, "request", request)
        result = controller.stop()
    assert injected and rejected, "Continued actual entry churn must reject the first snapshot"
    assert isinstance(result, ControlSuccess), result
    assert stop_requests == 1
    assert controller._child is not None and controller._child.poll() == 0


def test_controller_start_status_stop_and_repeat_start_use_one_real_owner(
    tmp_path_factory: pytest.TempPathFactory,
    owned_launches: None,
) -> None:
    root = tmp_path_factory.mktemp("life").resolve()
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    controller = RuntimeController(layout)
    try:
        before = controller.status()
        assert isinstance(before, ControlSuccess) and before.data.state.value == "stopped"
        assert not root.joinpath("r").exists()
        first = controller.start()
        assert isinstance(first, ControlSuccess), first
        assert first.data.control_ready and first.data.application_ready
        again = RuntimeController(layout).start()
        assert isinstance(again, ControlSuccess), again
        assert again.data.identity == first.data.identity
        assert controller.status().data == first.data
        stopped = RuntimeController(layout).stop()
        assert isinstance(stopped, ControlSuccess), stopped
        assert stopped.data.state.value == "stopped"
        assert layout.lock.exists() and layout.credential.exists()
        assert not layout.socket.exists()
        repeat = controller.stop()
        assert isinstance(repeat, ControlSuccess)
        assert layout.database.exists() and layout.settings.exists()
        assert stopped.data.storage.state == "not_checked"
    finally:
        result = controller.stop()
        if isinstance(result, Failure):
            pytest.fail("Owned lifecycle fixture cleanup was unconfirmed")
        layout.credential.unlink(missing_ok=True)


def test_concurrent_starts_converge_and_restart_preserves_anchor_and_credential(
    installation: RuntimeLayout,
) -> None:
    controllers = [RuntimeController(installation) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda controller: controller.start(), controllers))
    assert all(isinstance(result, ControlSuccess) for result in results), results
    identities = {result.data.identity for result in results if isinstance(result, ControlSuccess)}
    assert len(identities) == 1
    original_digest = hashlib.sha256(installation.credential.read_bytes()).digest()
    anchor = installation.lock.stat().st_ino
    assert isinstance(controllers[0].stop(), ControlSuccess)
    restarted = controllers[0].start()
    assert isinstance(restarted, ControlSuccess), restarted
    assert restarted.data.identity not in identities
    preserved = hashlib.sha256(installation.credential.read_bytes()).digest() == original_digest
    assert preserved
    assert installation.lock.stat().st_ino == anchor
