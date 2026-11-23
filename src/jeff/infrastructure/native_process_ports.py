"""Fixed macOS metadata probes; no process commands, control, or environment reads."""

import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path

from jeff.application.process_ports import (
    NativeListener,
    ProcessListData,
    ProcessListSuccess,
    ProcessPortsData,
    ProcessPortsSuccess,
)
from jeff.application.projects import ErrorData, Failure
from jeff.domain.projects import Project

_LIMIT = 32768
_MAX_LISTENERS = 64
_ADDRESS = re.compile(rb"127\.0\.0\.1:([0-9]{1,5})")
# Apple's bundled lsof does not support the optional device-cache flag (-D).
_LSOF = ("/usr/sbin/lsof", "-nP", "-a")


class _Unconfirmed(Exception):
    pass


def _query(argv: tuple[str, ...], deadline: float) -> tuple[int, bytes]:
    if time.monotonic() >= deadline:
        raise _Unconfirmed
    out = bytearray()
    with selectors.DefaultSelector() as selector:
        child = subprocess.Popen(
            argv,
            cwd="/",
            env={"HOME": "/var/empty", "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert child.stdout is not None and child.stderr is not None
            selector.register(child.stdout, selectors.EVENT_READ, "stdout")
            selector.register(child.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _Unconfirmed
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == "stderr" or len(out) + len(chunk) > _LIMIT:
                        raise _Unconfirmed
                    else:
                        out.extend(chunk)
            return child.wait(timeout=max(0, deadline - time.monotonic())), bytes(out)
        finally:
            try:
                if child.poll() is None:
                    child.kill()  # Only the captured metadata helper, not a discovered PID.
                    child.wait(timeout=1)
            finally:
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()


def _records(data: bytes) -> list[tuple[bytes, ...]]:
    if not data:
        return []
    if not data.endswith(b"\0\n"):
        raise _Unconfirmed
    records = [tuple(line.split(b"\0")) for line in data[:-2].split(b"\0\n")]
    if any(not field for row in records for field in row):
        raise _Unconfirmed
    return records


def _listeners(data: bytes) -> set[tuple[int, int]]:
    result: set[tuple[int, int]] = set()
    pid: int | None = None
    for row in _records(data):
        if row[0].startswith(b"p"):
            if len(row) != 1 or not re.fullmatch(rb"p[1-9][0-9]{0,9}", row[0]):
                raise _Unconfirmed
            pid = int(row[0][1:])
            if pid >= 2**31:
                raise _Unconfirmed
            continue
        addresses = [field[1:] for field in row[1:] if field.startswith(b"n")]
        if not row[0].startswith(b"f") or pid is None or len(addresses) != 1:
            raise _Unconfirmed
        match = _ADDRESS.fullmatch(addresses[0])
        if match is None:
            continue  # The OS query may also find a wildcard listener; not in our scope.
        port = int(match[1])
        if not 0 < port <= 65535 or b"TST=LISTEN" not in row:
            raise _Unconfirmed
        result.add((pid, port))
        if len(result) > _MAX_LISTENERS:
            raise _Unconfirmed
    return result


def _cwd(data: bytes, pid: int) -> Path:
    rows = _records(data)
    if len(rows) != 2 or rows[0] != (f"p{pid}".encode(),):
        raise _Unconfirmed
    files = rows[1]
    return _directory(files)


def _directory(files: tuple[bytes, ...]) -> Path:
    if len(files) != 2 or files[0] != b"fcwd" or not files[1].startswith(b"n/"):
        raise _Unconfirmed
    name = os.fsdecode(files[1][1:])
    path = Path(name)
    if str(path) != name or ".." in path.parts or "\n" in name or "\r" in name:
        raise _Unconfirmed
    return path


def _start(pid: int, uid: int, deadline: float) -> bytes:
    code, data = _query(("/bin/ps", "-p", str(pid), "-o", "uid=", "-o", "lstart="), deadline)
    row = data.strip().split(maxsplit=1)
    if code != 0 or len(row) != 2 or row[0] != str(uid).encode() or not re.fullmatch(
        rb"[A-Z][a-z]{2} [A-Z][a-z]{2} [ 0-9][0-9] [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}",
        row[1],
    ):
        raise _Unconfirmed
    return row[1]


def _scoped_listeners(uid: int, deadline: float) -> set[tuple[int, int]]:
    code, data = _query(
        (*_LSOF, "-u", str(uid), "-iTCP@127.0.0.1", "-sTCP:LISTEN", "-F0pfnT"), deadline
    )
    if code == 1 and not data:
        return set()
    if code != 0:
        raise _Unconfirmed
    return _listeners(data)


def _observe(pid: int, port: int, uid: int, deadline: float) -> Path:
    before = _start(pid, uid, deadline)
    directory = _query_cwd(pid, deadline)
    code, data = _query(
        (*_LSOF, "-p", str(pid), f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN", "-F0pfnT"),
        deadline,
    )
    if code != 0 or (pid, port) not in _listeners(data) or before != _start(pid, uid, deadline):
        raise _Unconfirmed
    return directory


def _query_cwd(pid: int, deadline: float) -> Path:
    code, data = _query((*_LSOF, "-p", str(pid), "-d", "cwd", "-F0pfn"), deadline)
    if code != 0:
        raise _Unconfirmed
    return _cwd(data, pid)


def _process_candidates(data: bytes, root: Path) -> tuple[dict[int, Path], int]:
    rows = _records(data)
    if len(rows) % 2 or len(rows) > 1024:
        raise _Unconfirmed
    candidates: dict[int, Path] = {}
    seen: set[int] = set()
    unconfirmed = 0
    for process, file in zip(rows[::2], rows[1::2], strict=True):
        if len(process) != 1 or not re.fullmatch(rb"p[1-9][0-9]{0,9}", process[0]):
            raise _Unconfirmed
        pid = int(process[0][1:])
        if pid >= 2**31 or pid in seen or file[0] != b"fcwd":
            raise _Unconfirmed
        seen.add(pid)
        try:
            cwd = _directory(file)
        except _Unconfirmed:
            unconfirmed += 1
        else:
            if cwd.is_relative_to(root):
                candidates[pid] = cwd
    if len(candidates) > 64:
        raise _Unconfirmed
    return candidates, unconfirmed


def read_process_list(project: Project, deadline: float) -> ProcessListSuccess | Failure:
    """Observe project cwd evidence independently of sockets or executable names."""
    deadline = min(deadline, time.monotonic() + 5)
    uid = os.geteuid()
    try:
        if sys.platform != "darwin":
            raise _Unconfirmed
        code, data = _query((*_LSOF, "-u", str(uid), "-d", "cwd", "-F0pfn"), deadline)
        if code != 0 and not (code == 1 and not data):
            raise _Unconfirmed
        candidates, unconfirmed = _process_candidates(data, project.root)
    except (_Unconfirmed, OSError, subprocess.SubprocessError):
        return Failure(
            error=ErrorData(
                code="process_list_unconfirmed", message="Could not inspect native processes."
            )
        )
    pids: list[int] = []
    for pid, cwd in sorted(candidates.items()):
        try:
            before = _start(pid, uid, deadline)
            if _query_cwd(pid, deadline) != cwd or _start(pid, uid, deadline) != before:
                raise _Unconfirmed
            pids.append(pid)
        except (_Unconfirmed, OSError, subprocess.SubprocessError):
            unconfirmed += 1
    return ProcessListSuccess(data=ProcessListData(pids=tuple(pids), unconfirmed=unconfirmed))


def read_process_ports(project: Project, deadline: float) -> ProcessPortsSuccess | Failure:
    """Report scoped observations, not launch provenance or reusable process identity."""
    deadline = min(deadline, time.monotonic() + 5)
    uid = os.geteuid()
    try:
        if sys.platform != "darwin":
            raise _Unconfirmed
        candidates = _scoped_listeners(uid, deadline)
    except (_Unconfirmed, OSError, subprocess.SubprocessError):
        return Failure(
            error=ErrorData(
                code="process_ports_unconfirmed", message="Could not inspect native loopback ports."
            )
        )
    listeners: list[NativeListener] = []
    unconfirmed = 0
    for pid, port in sorted(candidates):
        try:
            cwd = _observe(pid, port, uid, deadline)
        except (_Unconfirmed, OSError, subprocess.SubprocessError):
            unconfirmed += 1
        else:
            if cwd.is_relative_to(project.root):
                listeners.append(NativeListener(pid=pid, port=port))
    return ProcessPortsSuccess(
        data=ProcessPortsData(listeners=tuple(listeners), unconfirmed=unconfirmed)
    )
