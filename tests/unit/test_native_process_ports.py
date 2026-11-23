"""Native listener parsing and project attribution without starting processes."""

import os
import socket
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.process_ports import ProcessListSuccess, ProcessPortsSuccess
from jeff.domain.projects import Project
from jeff.infrastructure import native_process_ports as native


def _listener(pid: int, port: int) -> bytes:
    return f"p{pid}\0\nf3\0n127.0.0.1:{port}\0TST=LISTEN\0\n".encode()


def test_only_explicit_ipv4_loopback_listener_is_reported() -> None:
    wildcard = _listener(42, 8000).replace(b"127.0.0.1", b"*")
    assert native._listeners(_listener(41, 8000) + wildcard) == {(41, 8000)}


@pytest.mark.parametrize("data", [b"p5\0\nf3", _listener(42, 65536), _listener(42, 42)[:-1]])
def test_malformed_output_is_not_an_empty_inventory(data: bytes) -> None:
    with pytest.raises(native._Unconfirmed):
        native._listeners(data)


def test_attribution_excludes_unrelated_and_counts_changed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = f"{os.geteuid()} Fri Nov 21 11:00:00 2025\n".encode()
    calls: dict[int, int] = {}

    def query(argv: tuple[str, ...], deadline: float) -> tuple[int, bytes]:
        assert deadline > time.monotonic()
        if "-u" in argv:
            return 0, b"".join(_listener(pid, 8000 + pid) for pid in (41, 42, 43))
        pid = int(argv[argv.index("-p") + 1])
        if argv[0] == "/bin/ps":
            calls[pid] = calls.get(pid, 0) + 1
            if pid == 43 and calls[pid] > 1:
                return 0, start.replace(b"11:00:00", b"11:00:01")
            return 0, start
        if "cwd" in argv:
            cwd = "/project/subdir" if pid != 42 else "/project-lookalike"
            return 0, f"p{pid}\0\nfcwd\0n{cwd}\0\n".encode()
        return 0, _listener(pid, 8000 + pid)

    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native, "_query", query)
    reply = native.read_process_ports(Project(uuid4(), Path("/project")), time.monotonic() + 5)
    assert isinstance(reply, ProcessPortsSuccess)
    assert [(row.pid, row.port) for row in reply.data.listeners] == [(41, 8041)]
    assert reply.data.unconfirmed == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native metadata commands")
def test_real_adapter_finds_its_disposable_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    monkeypatch.chdir(root)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        reply = native.read_process_ports(Project(uuid4(), root), time.monotonic() + 5)
    assert isinstance(reply, ProcessPortsSuccess), reply
    assert (os.getpid(), port) in {(row.pid, row.port) for row in reply.data.listeners}

    # The listener is closed: process discovery does not depend on port presence.
    inventory = native.read_process_list(Project(uuid4(), root), time.monotonic() + 5)
    assert isinstance(inventory, ProcessListSuccess), inventory
    assert os.getpid() in inventory.data.pids


def test_process_list_excludes_outside_and_counts_missing_or_changed_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cwd(pid: int, path: str) -> bytes:
        return f"p{pid}\0\nfcwd\0n{path}\0\n".encode()

    def query(argv: tuple[str, ...], deadline: float) -> tuple[int, bytes]:
        assert deadline > time.monotonic()
        assert not any(arg.startswith("-i") for arg in argv)
        if "-u" in argv:
            return 0, (
                cwd(41, "/project") + cwd(42, "/project-unrelated") + cwd(43, "/project")
                + b"p44\0\nfcwd\0\n" + cwd(45, "/project")
            )
        pid = int(argv[argv.index("-p") + 1])
        assert pid in (41, 43, 45)
        if argv[0] == "/bin/ps":
            if pid == 45:
                return 1, b""
            return 0, f"{os.geteuid()} Fri Nov 21 11:00:00 2025\n".encode()
        return 0, cwd(pid, "/outside" if pid == 43 else "/project")

    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native, "_query", query)
    reply = native.read_process_list(Project(uuid4(), Path("/project")), time.monotonic() + 5)
    assert isinstance(reply, ProcessListSuccess)
    assert reply.data.pids == (41,)
    assert reply.data.unconfirmed == 3
