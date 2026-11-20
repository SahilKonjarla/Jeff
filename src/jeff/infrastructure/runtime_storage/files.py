"""Pinned database metadata, sidecar policy and declared rollback-journal eligibility."""

import os
import stat
from typing import TYPE_CHECKING

from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_state import RECOVERY_DATABASE_BYTES, RECOVERY_JOURNAL_BYTES

from .binding import _method

if TYPE_CHECKING:
    from .store import SQLiteStorage


_SIDECARS = ("state.sqlite3-journal", "state.sqlite3-wal", "state.sqlite3-shm")


_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink


@_method
def _authorize_recovery(self: "SQLiteStorage", root: int, database_fd: int) -> int:
    """Read-only eligibility before writable SQLite open; return a pinned journal FD."""
    journal = os.open("state.sqlite3-journal", os.O_RDONLY | _FLAGS, dir_fd=root)
    try:
        before = os.fstat(journal)
        permit = self.owner.verify_storage_recovery(database_fd, journal)
        database = os.fstat(database_fd)
        if not (
            0 < database.st_size <= RECOVERY_DATABASE_BYTES
            and 28 <= before.st_size <= RECOVERY_JOURNAL_BYTES
            and permit.original_pages * permit.page_size <= RECOVERY_DATABASE_BYTES
            and database.st_size % permit.page_size == 0
        ):
            raise StorageFault("storage_recovery_required")
        header = os.pread(journal, 28, 0)
        magic = bytes.fromhex("d9d505f920a163d7")
        if len(header) != 28 or header[:8] != magic:
            raise StorageFault("storage_recovery_required")
        records = int.from_bytes(header[8:12], "big")
        original_pages = int.from_bytes(header[16:20], "big")
        sector = int.from_bytes(header[20:24], "big")
        page_size = int.from_bytes(header[24:28], "big")
        if not (
            records > 0
            and original_pages == permit.original_pages
            and page_size == permit.page_size
            and 512 <= sector <= 65536
            and sector & (sector - 1) == 0
            and before.st_size >= sector + page_size + 8
            and (records == 0xFFFFFFFF or records <= (before.st_size - sector) // (page_size + 8))
        ):
            raise StorageFault("storage_recovery_required")
        # ATTACH is forbidden; never adopt a super-journal relationship/path.
        if os.pread(journal, 8, before.st_size - 8) == magic:
            raise StorageFault("storage_recovery_required")
        after = os.fstat(journal)
        if (_signature(before), before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            _signature(after),
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise StorageFault("storage_recovery_required")
        self.owner.verify_storage_recovery(database_fd, journal)
        return journal
    except BaseException:
        os.close(journal)
        raise


@_method
def _sidecars(self: "SQLiteStorage", root: int, *, preexisting: bool) -> None:
    for name in _SIDECARS:
        try:
            info = os.stat(name, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if preexisting:
            raise StorageFault("storage_recovery_required")
        if not (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == self.owner.store.layout.uid
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
        ):
            raise StorageFault("storage_unsafe")


@_method
def _check(self: "SQLiteStorage", root: int, fd: int) -> None:
    self.owner.check()
    current = os.stat("state.sqlite3", dir_fd=root, follow_symlinks=False)
    before = os.fstat(fd)
    if not (
        stat.S_ISREG(current.st_mode)
        and current.st_uid == self.owner.store.layout.uid
        and stat.S_IMODE(current.st_mode) == 0o600
        and current.st_nlink == 1
        and _signature(current) == _signature(before)
    ):
        raise StorageFault("storage_unsafe")
    self._sidecars(root, preexisting=False)
