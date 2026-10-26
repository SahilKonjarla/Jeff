"""Private runtime state and lifetime ownership. Never unlink the flock anchor."""

import fcntl
import os
import secrets
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Literal, Self, cast

from jeff.application.diagnostics import DiagnosticDeadlineExceeded
from jeff.application.projects import Failure
from jeff.application.runtime import ControlFault, ControlIssueCode, RuntimeLayout
from jeff.infrastructure.runtime_paths import inspect_layout

_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def _private_file(info: os.stat_result, uid: int, size: int) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == uid
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and info.st_size == size
    )


def _stable(chain: list[tuple[int, str, os.stat_result]]) -> None:
    for fd, name, before in chain:
        if _identity(before) != _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
            raise ControlFault("runtime_ownership_unconfirmed")


def _diagnostic_signature(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class DiagnosticLease:
    """Read-only shared lock, fixed-file reads and revalidation; no mutable Ownership."""

    def __init__(self, store: "RuntimeStateStore", fd: int, root: int, deadline: float) -> None:
        self._store = store
        self._fd = fd
        self._root = root
        self._deadline = deadline
        self._data_identity: tuple[int, ...] | None = None
        self._observed: dict[Literal["settings", "database"], tuple[int, ...]] = {}

    @property
    def layout(self) -> RuntimeLayout:
        return self._store.layout

    def check(self) -> None:
        if time.monotonic() >= self._deadline:
            raise DiagnosticDeadlineExceeded
        if self._fd < 0:
            raise ControlFault("runtime_ownership_unconfirmed")
        try:
            with self._store.directory(self.layout.runtime_dir, create=False) as root:
                if _identity(os.fstat(root)) != _identity(os.fstat(self._root)):
                    raise ControlFault("runtime_ownership_unconfirmed")
                held = os.fstat(self._fd)
                current = os.stat("daemon.lock", dir_fd=root, follow_symlinks=False)
                if not (
                    _private_file(held, self.layout.uid, 0)
                    and _private_file(current, self.layout.uid, 0)
                    and _identity(held) == _identity(current)
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                try:
                    os.stat("daemon.sock", dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ControlFault("runtime_endpoint_collision")
            if self._data_identity is not None:
                with self._store.directory(self.layout.data_dir, create=False) as root:
                    if _identity(os.fstat(root)) != self._data_identity:
                        raise ControlFault("runtime_ownership_unconfirmed")
                    credential = os.stat("ipc.key", dir_fd=root, follow_symlinks=False)
                    if not _private_file(credential, self.layout.uid, 32):
                        raise ControlFault("runtime_credentials_invalid")
                    for role, before in self._observed.items():
                        name = "settings.yaml" if role == "settings" else "state.sqlite3"
                        after = os.stat(name, dir_fd=root, follow_symlinks=False)
                        if before != _diagnostic_signature(after):
                            raise ControlFault("runtime_ownership_unconfirmed")
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None
        if time.monotonic() >= self._deadline:
            raise DiagnosticDeadlineExceeded

    @contextmanager
    def data_directory(self) -> Iterator[int]:
        self.check()
        with self._store.directory(self.layout.data_dir, create=False) as root:
            identity = _identity(os.fstat(root))
            if self._data_identity is not None and self._data_identity != identity:
                raise ControlFault("runtime_ownership_unconfirmed")
            self._data_identity = identity
            self.check()
            yield root
            self.check()

    @contextmanager
    def open_file(self, role: Literal["settings", "database"]) -> Iterator[int]:
        """Only declared files, O_RDONLY; observed identities remain checked until lease exit."""
        self.check()
        if role not in ("settings", "database"):
            raise ControlFault("invalid_runtime_path")
        name = "settings.yaml" if role == "settings" else "state.sqlite3"
        try:
            with self._store.directory(self.layout.data_dir, create=False) as root:
                identity = _identity(os.fstat(root))
                if self._data_identity is not None and self._data_identity != identity:
                    raise ControlFault("runtime_ownership_unconfirmed")
                self._data_identity = identity
                self.check()
                fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
                try:
                    before = os.fstat(fd)
                    if not (
                        stat.S_ISREG(before.st_mode)
                        and before.st_uid == self.layout.uid
                        and stat.S_IMODE(before.st_mode) == 0o600
                        and before.st_nlink == 1
                    ):
                        raise ControlFault("unsafe_runtime_path")
                    signature = _diagnostic_signature(before)
                    if role in self._observed and self._observed[role] != signature:
                        raise ControlFault("runtime_ownership_unconfirmed")
                    self._observed[role] = signature
                    self.check()
                    yield fd
                    if _diagnostic_signature(os.fstat(fd)) != signature:
                        raise ControlFault("runtime_ownership_unconfirmed")
                    self.check()
                finally:
                    os.close(fd)
        except OSError:
            raise ControlFault("runtime_inspection_failed") from None


class Ownership:
    """An actual acquired descriptor; close duplicates, NEVER unlock the shared description."""

    def __init__(self, store: "RuntimeStateStore", fd: int, directory_fd: int) -> None:
        self.store = store
        self.fd = fd
        self.directory_fd = directory_fd

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1

    def check(self) -> None:
        try:
            with self.store.directory(self.store.layout.runtime_dir, create=False) as root:
                if _identity(os.fstat(root)) != _identity(os.fstat(self.directory_fd)):
                    raise ControlFault("runtime_ownership_unconfirmed")
                descriptor = os.fstat(self.fd)
                entry = os.stat("daemon.lock", dir_fd=root, follow_symlinks=False)
                if not _private_file(descriptor, self.store.layout.uid, 0) or (
                    _identity(descriptor) != _identity(entry) or entry.st_nlink != 1
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None

    def require_no_endpoint(self) -> None:
        self.check()
        try:
            os.stat("daemon.sock", dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise ControlFault("runtime_inspection_failed") from None
        raise ControlFault("runtime_endpoint_collision")

    def endpoint_identity(self) -> tuple[int, int]:
        self.check()
        try:
            info = os.stat("daemon.sock", dir_fd=self.directory_fd, follow_symlinks=False)
            if not (
                stat.S_ISSOCK(info.st_mode)
                and info.st_uid == self.store.layout.uid
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1
            ):
                raise ControlFault("unsafe_runtime_path")
            return info.st_dev, info.st_ino
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None

    def remove_endpoint(self, identity: tuple[int, int]) -> None:
        self.check()
        try:
            if self.endpoint_identity() != identity:
                raise ControlFault("runtime_ownership_unconfirmed")
            os.unlink("daemon.sock", dir_fd=self.directory_fd)
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None


class RuntimeStateStore:
    def __init__(self, layout: RuntimeLayout) -> None:
        if sys.platform != "darwin":
            raise ControlFault("unsupported_runtime_platform")
        if layout.uid != os.geteuid():
            raise ControlFault("unsafe_runtime_path")
        files = (layout.database, layout.settings, layout.credential, layout.socket, layout.lock)
        if any(
            root.is_relative_to(file)
            for root in (layout.data_dir, layout.runtime_dir)
            for file in files
        ):
            raise ControlFault("invalid_runtime_path")
        report = inspect_layout(layout)
        if isinstance(report, Failure):
            raise ControlFault(cast(ControlIssueCode, report.error.code))
        if any(entry.state == "unsafe" for entry in report.entries):
            raise ControlFault("unsafe_runtime_path")
        if any(entry.state == "unknown" for entry in report.entries):
            raise ControlFault("runtime_inspection_failed")
        self.layout = layout

    @contextmanager
    def directory(self, path: Path, *, create: bool) -> Iterator[int]:
        """Operation-time no-follow directory traversal; no arbitrary link resolution."""
        if path not in (self.layout.data_dir, self.layout.runtime_dir):
            raise ControlFault("invalid_runtime_path")
        try:
            with ExitStack() as stack:
                flags = os.O_RDONLY | os.O_DIRECTORY | _FILE_FLAGS
                fd = os.open("/", flags)
                stack.callback(os.close, fd)
                chain: list[tuple[int, str, os.stat_result]] = []
                current = Path("/")
                for name in path.parts[1:]:
                    current /= name
                    try:
                        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        _stable(chain)
                        if not create:
                            raise ControlFault("runtime_absent") from None
                        try:
                            os.mkdir(name, mode=0o700, dir_fd=fd)
                        except FileExistsError:
                            pass
                        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if before.st_uid not in (0, self.layout.uid) or (
                        stat.S_IMODE(before.st_mode) & 0o022 and not before.st_mode & stat.S_ISVTX
                    ):
                        # Private leaves are not sufficient if another user can rename
                        # their ancestors and replace the lifetime-lock namespace.
                        raise ControlFault("unsafe_runtime_path")
                    if not stat.S_ISDIR(before.st_mode) or (
                        current in (self.layout.data_dir, self.layout.runtime_dir)
                        and (
                            before.st_uid != self.layout.uid
                            or stat.S_IMODE(before.st_mode) != 0o700
                        )
                    ):
                        raise ControlFault("unsafe_runtime_path")
                    next_fd = os.open(name, flags, dir_fd=fd)
                    stack.callback(os.close, next_fd)
                    if _identity(before) != _identity(os.fstat(next_fd)):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    chain.append((fd, name, before))
                    fd = next_fd
                info = os.fstat(fd)
                if info.st_uid != self.layout.uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ControlFault("unsafe_runtime_path")
                _stable(chain)
                yield fd
                _stable(chain)
        except OSError:
            raise ControlFault("runtime_inspection_failed") from None

    @contextmanager
    def diagnostic_lease(self, deadline: float) -> Iterator[DiagnosticLease]:
        """Non-creating O_RDONLY shared flock; never start, mint authority or unlink."""
        if time.monotonic() >= deadline:
            raise DiagnosticDeadlineExceeded
        with self.directory(self.layout.runtime_dir, create=False) as root:
            try:
                fd = os.open("daemon.lock", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
            except FileNotFoundError:
                raise ControlFault("runtime_absent") from None
            except OSError:
                raise ControlFault("runtime_inspection_failed") from None
            lease = DiagnosticLease(self, fd, root, deadline)
            try:
                info = os.fstat(fd)
                if not _private_file(info, self.layout.uid, 0):
                    raise ControlFault("unsafe_runtime_path")
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ControlFault("runtime_ownership_unconfirmed") from None
                lease.check()
                yield lease
                lease.check()
            except OSError:
                raise ControlFault("runtime_inspection_failed") from None
            finally:
                lease._fd = -1
                os.close(fd)

    def acquire(self, *, create: bool) -> Ownership | None:
        """None means a real conflicting flock; absence is a separate safe fault."""
        with ExitStack() as failed_ownership:
            with self.directory(self.layout.runtime_dir, create=create) as root:
                fd = -1
                try:
                    try:
                        fd = os.open("daemon.lock", os.O_RDWR | _FILE_FLAGS, dir_fd=root)
                    except FileNotFoundError:
                        try:
                            os.stat("daemon.sock", dir_fd=root, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise ControlFault("runtime_endpoint_collision") from None
                        if not create:
                            raise ControlFault("runtime_absent") from None
                        try:
                            fd = os.open(
                                "daemon.lock",
                                os.O_RDWR | _FILE_FLAGS | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=root,
                            )
                        except FileExistsError:
                            fd = os.open("daemon.lock", os.O_RDWR | _FILE_FLAGS, dir_fd=root)
                    info = os.fstat(fd)
                    if not _private_file(info, self.layout.uid, 0):
                        raise ControlFault("unsafe_runtime_path")
                    if _identity(info) != _identity(
                        os.stat("daemon.lock", dir_fd=root, follow_symlinks=False)
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return None
                    owner = Ownership(self, fd, os.dup(root))
                    failed_ownership.callback(owner.close)
                    fd = -1
                    owner.check()
                except OSError:
                    raise ControlFault("runtime_inspection_failed") from None
                finally:
                    if fd >= 0:
                        os.close(fd)
            # Transfer only after directory-context exit revalidation also succeeds.
            failed_ownership.pop_all()
            return owner

    def adopt(self, fd: int) -> Ownership:
        """Validate the inherited live lock, without creating any state or acquiring a new owner."""
        owner: Ownership | None = None
        try:
            with self.directory(self.layout.runtime_dir, create=False) as root:
                info = os.fstat(fd)
                if not _private_file(info, self.layout.uid, 0):
                    raise ControlFault("runtime_ownership_unconfirmed")
                owner = Ownership(self, fd, os.dup(root))
                owner.check()
                probe = self.acquire(create=False)
                if probe is not None:
                    probe.close()
                    raise ControlFault("runtime_ownership_unconfirmed")
                # An independent owner must conflict; our inherited description must not.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return owner
        except (OSError, ControlFault):
            if owner is not None:
                owner.close()
            raise ControlFault("runtime_ownership_unconfirmed") from None

    def credential(self, owner: Ownership) -> bytes:
        if owner.store is not self:
            raise ControlFault("runtime_ownership_unconfirmed")
        owner.require_no_endpoint()
        with self.directory(self.layout.data_dir, create=True) as root:
            try:
                fd = os.open(
                    "ipc.key",
                    os.O_WRONLY | _FILE_FLAGS | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root,
                )
            except FileExistsError:
                return self.read_credential()
            except OSError:
                raise ControlFault("runtime_credentials_invalid") from None
            try:
                value = secrets.token_bytes(32)
                offset = 0
                while offset < len(value):
                    written = os.write(fd, value[offset:])
                    if written <= 0:
                        raise ControlFault("runtime_credentials_invalid")
                    offset += written
                os.fsync(fd)
            except OSError:
                raise ControlFault("runtime_credentials_invalid") from None
            finally:
                os.close(fd)
        owner.check()
        return self.read_credential()

    def read_credential(self) -> bytes:
        try:
            with self.directory(self.layout.data_dir, create=False) as root:
                fd = os.open("ipc.key", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
                try:
                    before = os.fstat(fd)
                    if not _private_file(before, self.layout.uid, 32):
                        raise ControlFault("runtime_credentials_invalid")
                    value = os.read(fd, 33)
                    after = os.stat("ipc.key", dir_fd=root, follow_symlinks=False)
                    if (
                        len(value) != 32
                        or not _private_file(after, self.layout.uid, 32)
                        or (_identity(before) != _identity(after))
                    ):
                        raise ControlFault("runtime_credentials_invalid")
                    return value
                finally:
                    os.close(fd)
        except (OSError, ControlFault):
            raise ControlFault("runtime_credentials_invalid") from None
