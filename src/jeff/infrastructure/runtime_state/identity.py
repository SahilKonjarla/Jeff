"""Exact Darwin identity ABI and no-follow private metadata/chain checks."""

import ctypes
import os
import stat
import sys

from pydantic import ValidationError

from jeff.application.runtime import ControlFault

from .proofs import _FileIdentity

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
