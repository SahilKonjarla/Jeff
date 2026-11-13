"""Private runtime state and lifetime ownership. Never unlink the flock anchor."""

import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import socket
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from jeff.application.diagnostics import DiagnosticDeadlineExceeded
from jeff.application.projects import Failure
from jeff.application.runtime import ControlFault, ControlIssueCode, RuntimeLayout
from jeff.infrastructure.runtime_paths import inspect_layout

_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class _Timespec(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_int64), ("nanoseconds", ctypes.c_int64)]


class _DarwinStat(ctypes.Structure):
    # LP64 sys/stat.h __DARWIN_STRUCT_STAT64; compiled SDK oracle in6b-abi.json.
    _fields_ = [
        ("device", ctypes.c_int32),
        ("mode", ctypes.c_uint16),
        ("links", ctypes.c_uint16),
        ("inode", ctypes.c_uint64),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("rdev", ctypes.c_int32),
        ("access", _Timespec),
        ("modified", _Timespec),
        ("changed", _Timespec),
        ("birth", _Timespec),
        ("size", ctypes.c_int64),
        ("blocks", ctypes.c_int64),
        ("blocksize", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("generation", ctypes.c_uint32),
        ("spare", ctypes.c_int32),
        ("reserved", ctypes.c_int64 * 2),
    ]


class _ProofModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class _FileIdentity(_ProofModel):
    device: StrictInt = Field(ge=-(1 << 31), le=(1 << 32) - 1)
    inode: StrictInt = Field(gt=0, le=(1 << 64) - 1)
    birth_ns: StrictInt = Field(gt=0, le=(1 << 63) - 1)


class _EndpointBindings(_ProofModel):
    runtime: _FileIdentity
    data: _FileIdentity
    lock: _FileIdentity
    key: _FileIdentity
    socket: _FileIdentity
    receipt: _FileIdentity


class _EndpointReceipt(_ProofModel):
    version: StrictInt = Field(ge=1, le=1)
    instance_id: str = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    bindings: _EndpointBindings
    mac: str = Field(pattern=r"^[0-9a-f]{64}$")


class _RecoveryBindings(_ProofModel):
    runtime: _FileIdentity
    data: _FileIdentity
    lock: _FileIdentity
    key: _FileIdentity
    database: _FileIdentity
    journal: _FileIdentity
    permit: _FileIdentity


class _RecoveryPermit(_ProofModel):
    version: StrictInt = Field(ge=1, le=1)
    schema_version: StrictInt = Field(ge=4, le=5)
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    page_size: StrictInt = Field(ge=512, le=65536)
    original_pages: StrictInt = Field(gt=0, le=(1 << 32) - 1)
    bindings: _RecoveryBindings
    mac: str = Field(pattern=r"^[0-9a-f]{64}$")


RECOVERY_DATABASE_BYTES = 256 * 1024 * 1024
RECOVERY_JOURNAL_BYTES = 16 * 1024 * 1024


def _recovery_tag(permit: _RecoveryPermit, token: bytes) -> str:
    payload = json.dumps(
        permit.model_dump(exclude={"mac"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(token, b"jeff.storage.recovery.v1\0" + payload, hashlib.sha256).hexdigest()


def _recovery_entry(root: int, uid: int) -> os.stat_result | None:
    try:
        info = os.stat("recovery.json", dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == uid
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and 0 < info.st_size <= 2048
    ):
        raise ControlFault("unsafe_runtime_path")
    return info


def _native_identity(fd: int, name: str | None = None) -> _FileIdentity:
    """Exact no-follow Darwin birth timestamp, checked against Python's common fields."""
    if not (
        sys.platform == "darwin"
        and ctypes.sizeof(ctypes.c_void_p) == 8
        and ctypes.sizeof(_DarwinStat) == 144
        and _DarwinStat.birth.offset == 80
        and _DarwinStat.size.offset == 96
    ):
        raise ControlFault("unsupported_runtime_platform")
    if name is not None and name not in ("daemon.sock", "daemon.lock", "ipc.key", "endpoint.json"):
        raise ControlFault("invalid_runtime_path")
    try:
        before = os.fstat(fd) if name is None else os.stat(name, dir_fd=fd, follow_symlinks=False)
        libc = ctypes.CDLL(None, use_errno=True)
        result = _DarwinStat()
        if name is None:
            function = libc.fstat
            function.argtypes = [ctypes.c_int, ctypes.POINTER(_DarwinStat)]
            function.restype = ctypes.c_int
            status = function(fd, ctypes.byref(result))
        else:
            function = libc.fstatat
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.POINTER(_DarwinStat),
                ctypes.c_int,
            ]
            function.restype = ctypes.c_int
            status = function(fd, os.fsencode(name), ctypes.byref(result), 0x0020)
        if status != 0:
            raise ControlFault("runtime_inspection_failed")
        native_identity = (result.device, result.inode, result.mode, result.uid)
        if native_identity != _identity(before) or not 0 <= result.birth.nanoseconds < 1000000000:
            raise ControlFault("runtime_ownership_unconfirmed")
        if (result.links, result.size) != (before.st_nlink, before.st_size):
            # SQL may create entries between the two real stat calls. A directory's
            # link count/size is not its identity. Keep every ABI/common-field check:
            # accept only if one fresh observation matches this exact native sample.
            # Never resample changed identity/security fields or a mutable file body.
            if not stat.S_ISDIR(before.st_mode):
                raise ControlFault("runtime_ownership_unconfirmed")
            after = (
                os.fstat(fd) if name is None else os.stat(name, dir_fd=fd, follow_symlinks=False)
            )
            if (*_identity(after), after.st_nlink, after.st_size) != (
                *native_identity,
                result.links,
                result.size,
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
        return _FileIdentity(
            device=result.device,
            inode=result.inode,
            birth_ns=result.birth.seconds * 1000000000 + result.birth.nanoseconds,
        )
    except (OSError, AttributeError, ValidationError):
        raise ControlFault("runtime_inspection_failed") from None


def _receipt_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate receipt field")
        result[key] = value
    return result


def _receipt_tag(receipt: _EndpointReceipt, token: bytes) -> str:
    payload = json.dumps(
        receipt.model_dump(exclude={"mac"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(token, b"jeff.endpoint.receipt.v1\0" + payload, hashlib.sha256).hexdigest()


def _receipt_entry(root: int, uid: int) -> os.stat_result | None:
    try:
        info = os.stat("endpoint.json", dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == uid
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and 0 < info.st_size <= 2048
    ):
        raise ControlFault("unsafe_runtime_path")
    return info


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
                if _receipt_entry(root, self.layout.uid) is not None:
                    raise ControlFault("runtime_endpoint_collision")
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
                    if _recovery_entry(root, self.layout.uid) is not None:
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


class DiagnosticObservation:
    """Pinned existing lock namespace; free holds SH, held never impersonates ownership."""

    def __init__(
        self, store: "RuntimeStateStore", fd: int, root: int, deadline: float, held: bool
    ) -> None:
        self._store, self._fd, self._root = store, fd, root
        self._deadline, self._held = deadline, held
        self._issued_lease: DiagnosticLease | None = None
        self._endpoint = self._endpoint_identity()
        receipt = _receipt_entry(self._root, self._store.layout.uid)
        self._receipt = _diagnostic_signature(receipt) if receipt is not None else None

    @property
    def held(self) -> bool:
        return self._held

    @property
    def has_endpoint(self) -> bool:
        return self._endpoint is not None

    def _endpoint_identity(self) -> tuple[int, ...] | None:
        try:
            info = os.stat("daemon.sock", dir_fd=self._root, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not (
            stat.S_ISSOCK(info.st_mode)
            and info.st_uid == self._store.layout.uid
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
        ):
            raise ControlFault("unsafe_runtime_path")
        return _identity(info)

    def check(self) -> None:
        if time.monotonic() >= self._deadline:
            raise DiagnosticDeadlineExceeded
        if self._fd < 0:
            raise ControlFault("runtime_ownership_unconfirmed")
        try:
            with self._store.directory(self._store.layout.runtime_dir, create=False) as root:
                if _identity(os.fstat(root)) != _identity(os.fstat(self._root)):
                    raise ControlFault("runtime_ownership_unconfirmed")
                held = os.fstat(self._fd)
                current = os.stat("daemon.lock", dir_fd=root, follow_symlinks=False)
                if not (
                    _private_file(held, self._store.layout.uid, 0)
                    and _private_file(current, self._store.layout.uid, 0)
                    and _identity(held) == _identity(current)
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                if self._endpoint_identity() != self._endpoint:
                    raise ControlFault("runtime_ownership_unconfirmed")
                receipt = _receipt_entry(self._root, self._store.layout.uid)
                signature = _diagnostic_signature(receipt) if receipt is not None else None
                if signature != self._receipt:
                    raise ControlFault("runtime_ownership_unconfirmed")
                if self.held:
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        # This description now holds SH; context close releases ONLY our copy.
                        raise ControlFault("runtime_ownership_unconfirmed")
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None
        if time.monotonic() >= self._deadline:
            raise DiagnosticDeadlineExceeded

    def stopped_lease(self) -> DiagnosticLease:
        """Issue read-only authority only while the pinned lock is free and residue absent."""
        self.check()
        if self.held:
            raise ControlFault("runtime_ownership_unconfirmed")
        if self._endpoint is not None or self._receipt is not None:
            raise ControlFault("runtime_endpoint_collision")
        if self._issued_lease is None:
            self._issued_lease = DiagnosticLease(self._store, self._fd, self._root, self._deadline)
        return self._issued_lease


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
        """Close this copy only; LOCK_UN would also release inherited live copies."""
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1

    def check(self) -> None:
        """Revalidate that the captured descriptors still name the selected stable anchor."""
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

    def _storage_file(self, root: int, name: str, descriptor: int) -> _FileIdentity:
        held = os.fstat(descriptor)
        current = os.stat(name, dir_fd=root, follow_symlinks=False)
        if not (
            _private_file(held, self.store.layout.uid, held.st_size)
            and _private_file(current, self.store.layout.uid, held.st_size)
            and _identity(held) == _identity(current)
        ):
            raise ControlFault("runtime_ownership_unconfirmed")
        return _native_identity(descriptor)

    def _storage_bindings(
        self, root: int, database_fd: int, journal: _FileIdentity, permit_fd: int
    ) -> _RecoveryBindings:
        self.check()
        key = os.stat("ipc.key", dir_fd=root, follow_symlinks=False)
        if not _private_file(key, self.store.layout.uid, 32):
            raise ControlFault("runtime_credentials_invalid")
        return _RecoveryBindings(
            runtime=_native_identity(self.directory_fd),
            data=_native_identity(root),
            lock=_native_identity(self.fd),
            key=_native_identity(root, "ipc.key"),
            database=self._storage_file(root, "state.sqlite3", database_fd),
            journal=journal,
            permit=_native_identity(permit_fd),
        )

    def _read_storage_permit(self, root: int, fd: int, token: bytes) -> _RecoveryPermit:
        self.check()
        before = os.fstat(fd)
        entry = _recovery_entry(root, self.store.layout.uid)
        if entry is None or _diagnostic_signature(before) != _diagnostic_signature(entry):
            raise ControlFault("runtime_ownership_unconfirmed")
        os.lseek(fd, 0, os.SEEK_SET)
        payload = os.read(fd, 2049)
        try:
            permit = _RecoveryPermit.model_validate(
                json.loads(payload, object_pairs_hook=_receipt_pairs)
            )
            canonical = json.dumps(
                permit.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            if (
                len(payload) != before.st_size
                or payload != canonical
                or not hmac.compare_digest(permit.mac, _recovery_tag(permit, token))
                or permit.page_size & (permit.page_size - 1)
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
        except (ValueError, ValidationError, RecursionError):
            raise ControlFault("runtime_ownership_unconfirmed") from None
        if _diagnostic_signature(os.fstat(fd)) != _diagnostic_signature(before):
            raise ControlFault("runtime_ownership_unconfirmed")
        return permit

    def arm_storage_recovery(
        self,
        database_fd: int,
        journal_fd: int,
        *,
        page_size: int,
        original_pages: int,
        schema_version: int = 4,
    ) -> _RecoveryPermit:
        """Bind the actual SQLite-created journal before any business DML; never create it."""
        self.check()
        if type(schema_version) is not int or schema_version not in (4, 5):
            raise ControlFault("runtime_ownership_unconfirmed")
        token = self.store.read_credential()
        with self.store.directory(self.store.layout.data_dir, create=False) as root:
            if _recovery_entry(root, self.store.layout.uid) is not None:
                raise ControlFault("runtime_endpoint_collision")
            journal = self._storage_file(root, "state.sqlite3-journal", journal_fd)
            temporary = ".recovery-" + secrets.token_hex(16)
            fd = os.open(
                temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600, dir_fd=root
            )
            original = os.fstat(fd)
            try:
                bindings = self._storage_bindings(root, database_fd, journal, fd)
                permit = _RecoveryPermit(
                    version=1,
                    schema_version=schema_version,
                    nonce=secrets.token_hex(16),
                    page_size=page_size,
                    original_pages=original_pages,
                    bindings=bindings,
                    mac="0" * 64,
                )
                if page_size & (page_size - 1):
                    raise ControlFault("runtime_ownership_unconfirmed")
                permit = permit.model_copy(update={"mac": _recovery_tag(permit, token)})
                payload = json.dumps(
                    permit.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode("ascii")
                if not 0 < len(payload) <= 2048:
                    raise ControlFault("runtime_ownership_unconfirmed")
                offset = 0
                while offset < len(payload):
                    count = os.write(fd, payload[offset:])
                    if count <= 0:
                        raise ControlFault("runtime_inspection_failed")
                    offset += count
                os.fsync(fd)
                if (
                    not hmac.compare_digest(token, self.store.read_credential())
                    or self._storage_file(root, "state.sqlite3-journal", journal_fd) != journal
                    or self._storage_bindings(root, database_fd, journal, fd) != bindings
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                os.link(
                    temporary,
                    "recovery.json",
                    src_dir_fd=root,
                    dst_dir_fd=root,
                    follow_symlinks=False,
                )
                if _identity(os.stat(temporary, dir_fd=root, follow_symlinks=False)) != _identity(
                    original
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                os.unlink(temporary, dir_fd=root)
                os.fsync(root)
                if self._read_storage_permit(root, fd, token) != permit:
                    raise ControlFault("runtime_ownership_unconfirmed")
                return permit
            finally:
                try:
                    if _identity(
                        os.stat(temporary, dir_fd=root, follow_symlinks=False)
                    ) == _identity(original):
                        os.unlink(temporary, dir_fd=root)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(fd)

    def verify_storage_recovery(self, database_fd: int, journal_fd: int) -> _RecoveryPermit:
        """Authenticate exact namespace/file bindings; leave journal recovery to SQLite."""
        self.check()
        token = self.store.read_credential()
        with self.store.directory(self.store.layout.data_dir, create=False) as root:
            fd = os.open("recovery.json", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
            try:
                permit = self._read_storage_permit(root, fd, token)
                journal = self._storage_file(root, "state.sqlite3-journal", journal_fd)
                if self._storage_bindings(root, database_fd, journal, fd) != permit.bindings:
                    raise ControlFault("runtime_ownership_unconfirmed")
                if not hmac.compare_digest(token, self.store.read_credential()):
                    raise ControlFault("runtime_credentials_invalid")
                return permit
            finally:
                os.close(fd)

    def retire_storage_recovery(self, database_fd: int) -> None:
        """Retire only matching self-bound permission after SQLite removed its journal."""
        self.check()
        with self.store.directory(self.store.layout.data_dir, create=False) as root:
            if _recovery_entry(root, self.store.layout.uid) is None:
                return
            try:
                os.stat("state.sqlite3-journal", dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ControlFault("runtime_ownership_unconfirmed")
            token = self.store.read_credential()
            fd = os.open("recovery.json", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
            try:
                permit = self._read_storage_permit(root, fd, token)
                if (
                    self._storage_bindings(root, database_fd, permit.bindings.journal, fd)
                    != permit.bindings
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                if not hmac.compare_digest(token, self.store.read_credential()):
                    raise ControlFault("runtime_credentials_invalid")
                self.check()
                os.unlink("recovery.json", dir_fd=root)
                os.fsync(root)
            finally:
                os.close(fd)

    def require_no_endpoint(self) -> None:
        self.check()
        try:
            if _receipt_entry(self.directory_fd, self.store.layout.uid) is not None:
                raise ControlFault("runtime_endpoint_collision")
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

    def _endpoint_bindings(
        self, receipt_fd: int, socket_identity: _FileIdentity | None = None
    ) -> _EndpointBindings:
        self.check()
        with self.store.directory(self.store.layout.data_dir, create=False) as data:
            key = os.stat("ipc.key", dir_fd=data, follow_symlinks=False)
            if not _private_file(key, self.store.layout.uid, 32):
                raise ControlFault("runtime_credentials_invalid")
            return _EndpointBindings(
                runtime=_native_identity(self.directory_fd),
                data=_native_identity(data),
                lock=_native_identity(self.fd),
                key=_native_identity(data, "ipc.key"),
                socket=socket_identity or _native_identity(self.directory_fd, "daemon.sock"),
                receipt=_native_identity(receipt_fd),
            )

    def publish_endpoint(
        self, instance_id: UUID, token: bytes, *, endpoint: tuple[int, int] | None = None
    ) -> _EndpointReceipt:
        """Publish a signed actual binding without overwriting any existing receipt."""
        self.check()
        if _receipt_entry(self.directory_fd, self.store.layout.uid) is not None:
            raise ControlFault("runtime_endpoint_collision")
        if not hmac.compare_digest(token, self.store.read_credential()):
            raise ControlFault("runtime_credentials_invalid")
        expected = endpoint or self.endpoint_identity()
        temporary = ".endpoint-" + secrets.token_hex(16)
        fd = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
            0o600,
            dir_fd=self.directory_fd,
        )
        original = os.fstat(fd)
        try:
            bindings = self._endpoint_bindings(fd)
            if (bindings.socket.device, bindings.socket.inode) != expected:
                raise ControlFault("runtime_ownership_unconfirmed")
            receipt = _EndpointReceipt(
                version=1,
                instance_id=str(instance_id),
                nonce=secrets.token_hex(16),
                bindings=bindings,
                mac="0" * 64,
            )
            receipt = receipt.model_copy(update={"mac": _receipt_tag(receipt, token)})
            payload = json.dumps(
                receipt.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            if not 0 < len(payload) <= 2048:
                raise ControlFault("runtime_ownership_unconfirmed")
            offset = 0
            while offset < len(payload):
                count = os.write(fd, payload[offset:])
                if count <= 0:
                    raise ControlFault("runtime_inspection_failed")
                offset += count
            os.fsync(fd)
            self.check()
            if self._endpoint_bindings(fd) != bindings:
                raise ControlFault("runtime_ownership_unconfirmed")
            # link is atomic/no-clobber; nlink2 is never declared ready or recoverable.
            os.link(
                temporary,
                "endpoint.json",
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            if _identity(
                os.stat(temporary, dir_fd=self.directory_fd, follow_symlinks=False)
            ) != _identity(original):
                raise ControlFault("runtime_ownership_unconfirmed")
            os.unlink(temporary, dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
            if self._read_endpoint_receipt(fd, token) != receipt:
                raise ControlFault("runtime_ownership_unconfirmed")
            return receipt
        except OSError:
            raise ControlFault("runtime_inspection_failed") from None
        finally:
            try:
                current = os.stat(temporary, dir_fd=self.directory_fd, follow_symlinks=False)
                if _identity(current) == _identity(original):
                    os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(fd)

    def _read_endpoint_receipt(self, fd: int, token: bytes) -> _EndpointReceipt:
        self.check()
        if not hmac.compare_digest(token, self.store.read_credential()):
            raise ControlFault("runtime_credentials_invalid")
        before = os.fstat(fd)
        entry = _receipt_entry(self.directory_fd, self.store.layout.uid)
        if entry is None or _diagnostic_signature(before) != _diagnostic_signature(entry):
            raise ControlFault("runtime_ownership_unconfirmed")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            payload = os.read(fd, 2049)
            if len(payload) != before.st_size:
                raise ControlFault("runtime_ownership_unconfirmed")
            receipt = _EndpointReceipt.model_validate(
                json.loads(payload, object_pairs_hook=_receipt_pairs)
            )
            canonical = json.dumps(
                receipt.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            if payload != canonical or not hmac.compare_digest(
                receipt.mac, _receipt_tag(receipt, token)
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
        except (ValueError, ValidationError, RecursionError):
            raise ControlFault("runtime_ownership_unconfirmed") from None
        if _diagnostic_signature(os.fstat(fd)) != _diagnostic_signature(before):
            raise ControlFault("runtime_ownership_unconfirmed")
        return receipt

    def prepare_endpoint(self, deadline: float) -> None:
        """Explicit start only: retire proven stale entries under this exclusive owner."""
        self.check()
        if time.monotonic() >= deadline:
            raise ControlFault("runtime_start_timeout")
        receipt_entry = _receipt_entry(self.directory_fd, self.store.layout.uid)
        try:
            os.stat("daemon.sock", dir_fd=self.directory_fd, follow_symlinks=False)
            has_socket = True
        except FileNotFoundError:
            has_socket = False
        if receipt_entry is None:
            if has_socket:
                raise ControlFault("runtime_endpoint_collision")
            return
        token = self.store.read_credential()  # never create/rekey when residue exists
        fd = os.open("endpoint.json", os.O_RDONLY | _FILE_FLAGS, dir_fd=self.directory_fd)
        try:
            receipt = self._read_endpoint_receipt(fd, token)
            expected = self._endpoint_bindings(fd, None if has_socket else receipt.bindings.socket)
            if expected != receipt.bindings:
                raise ControlFault("runtime_ownership_unconfirmed")
            if has_socket:
                self.endpoint_identity()  # enforce type/mode/UID/link policy, not just identity
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlFault("runtime_start_timeout")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(min(0.25, remaining))
                    try:
                        probe.connect(str(self.store.layout.socket))
                    except OSError as error:
                        if error.errno != errno.ECONNREFUSED:
                            raise ControlFault("runtime_ownership_unconfirmed") from None
                    else:
                        raise ControlFault("runtime_endpoint_collision")
                self.check()
                if (
                    self._read_endpoint_receipt(fd, token) != receipt
                    or self._endpoint_bindings(fd) != expected
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
                self.endpoint_identity()
                if time.monotonic() >= deadline:
                    raise ControlFault("runtime_start_timeout")
                os.unlink("daemon.sock", dir_fd=self.directory_fd)
                os.fsync(self.directory_fd)
            self.check()
            try:
                os.stat("daemon.sock", dir_fd=self.directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ControlFault("runtime_endpoint_collision")
            if (
                self._read_endpoint_receipt(fd, token) != receipt
                or self._endpoint_bindings(fd, receipt.bindings.socket) != expected
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            if time.monotonic() >= deadline:
                raise ControlFault("runtime_start_timeout")
            os.unlink("endpoint.json", dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
            self.require_no_endpoint()
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None
        finally:
            os.close(fd)

    def remove_endpoint(
        self,
        identity: tuple[int, int],
        *,
        receipt: _EndpointReceipt | None = None,
        token: bytes | None = None,
    ) -> None:
        """Current binding authority only; a published receipt must still match exactly."""
        self.check()
        try:
            if self.endpoint_identity() != identity:
                raise ControlFault("runtime_ownership_unconfirmed")
            with ExitStack() as stack:
                proof_fd = -1
                if receipt is not None:
                    if token is None:
                        raise ControlFault("runtime_credentials_invalid")
                    proof_fd = os.open(
                        "endpoint.json", os.O_RDONLY | _FILE_FLAGS, dir_fd=self.directory_fd
                    )
                    stack.callback(os.close, proof_fd)
                    if (
                        self._read_endpoint_receipt(proof_fd, token) != receipt
                        or self._endpoint_bindings(proof_fd) != receipt.bindings
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                self.check()
                if self.endpoint_identity() != identity:
                    raise ControlFault("runtime_ownership_unconfirmed")
                os.unlink("daemon.sock", dir_fd=self.directory_fd)
                if receipt is not None:
                    assert token is not None
                    if (
                        self._read_endpoint_receipt(proof_fd, token) != receipt
                        or self._endpoint_bindings(proof_fd, receipt.bindings.socket)
                        != receipt.bindings
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    os.unlink("endpoint.json", dir_fd=self.directory_fd)
                    os.fsync(self.directory_fd)
        except OSError:
            raise ControlFault("runtime_ownership_unconfirmed") from None


class RuntimeStateStore:
    """Operate on one checked namespace; constructor metadata is not lifetime authority.

    Ownership, diagnostic leases and existing-key reads are separate capabilities.
    Missing state never authorizes reset, key rotation or a guessed-PID cleanup.
    """

    def __init__(self, layout: RuntimeLayout) -> None:
        if sys.platform != "darwin":
            raise ControlFault("unsupported_runtime_platform")
        if layout.uid != os.geteuid():
            raise ControlFault("unsafe_runtime_path")
        files = (
            layout.database,
            layout.settings,
            layout.credential,
            layout.socket,
            layout.lock,
            layout.endpoint_receipt,
            layout.recovery_permit,
        )
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
    def diagnostic_observation(self, deadline: float) -> Iterator[DiagnosticObservation]:
        """Non-creating O_RDONLY held/free observation; not mutable lifetime authority."""
        if time.monotonic() >= deadline:
            raise DiagnosticDeadlineExceeded
        with self.directory(self.layout.runtime_dir, create=False) as root:
            try:
                fd = os.open("daemon.lock", os.O_RDONLY | _FILE_FLAGS, dir_fd=root)
            except FileNotFoundError:
                raise ControlFault("runtime_absent") from None
            except OSError:
                raise ControlFault("runtime_inspection_failed") from None
            observation: DiagnosticObservation | None = None
            try:
                info = os.fstat(fd)
                if not _private_file(info, self.layout.uid, 0):
                    raise ControlFault("unsafe_runtime_path")
                held = False
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    held = True
                observation = DiagnosticObservation(self, fd, root, deadline, held)
                observation.check()
                yield observation
                observation.check()
            except OSError:
                raise ControlFault("runtime_inspection_failed") from None
            finally:
                if observation is not None:
                    observation._fd = -1
                    if observation._issued_lease is not None:
                        observation._issued_lease._fd = -1
                os.close(fd)

    @contextmanager
    def diagnostic_lease(self, deadline: float) -> Iterator[DiagnosticLease]:
        """Non-creating shared lease retained continuously through stopped reads."""
        with self.diagnostic_observation(deadline) as observation:
            lease = observation.stopped_lease()
            try:
                yield lease
                lease.check()
            finally:
                lease._fd = -1

    def acquire(self, *, create: bool) -> Ownership | None:
        """None means a real conflicting flock; absence is a separate safe fault."""
        with ExitStack() as failed_ownership:
            with self.directory(self.layout.runtime_dir, create=create) as root:
                fd = -1
                try:
                    try:
                        fd = os.open("daemon.lock", os.O_RDWR | _FILE_FLAGS, dir_fd=root)
                    except FileNotFoundError:
                        if _receipt_entry(root, self.layout.uid) is not None:
                            raise ControlFault("runtime_endpoint_collision") from None
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
        """Create only an absent key under ownership; never replace invalid existing bytes."""
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
        """Read the existing bounded private key without creating or repairing state."""
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
