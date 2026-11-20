"""No-follow open flags, finite snapshot capacity and identity/deadline checks."""

import math
import os
import time
import unicodedata

from jeff.application.execution import PreparedInputFault

_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


_DIR = os.O_RDONLY | os.O_DIRECTORY | _FLAGS


_SLOTS = 17


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _deadline(deadline: float) -> None:
    if not math.isfinite(deadline) or time.monotonic() >= deadline:
        raise PreparedInputFault("input_timeout")


def _names(fd: int, expected: set[str], deadline: float) -> None:
    # Only inspect expected snapshot directories, never recursively discover source files.
    normalized = {unicodedata.normalize("NFC", name) for name in expected}
    seen: set[str] = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            _deadline(deadline)
            name = unicodedata.normalize("NFC", entry.name)
            if name not in normalized or name in seen:
                raise PreparedInputFault("input_unsafe")
            seen.add(name)
    if seen != normalized:
        raise PreparedInputFault("input_changed")
