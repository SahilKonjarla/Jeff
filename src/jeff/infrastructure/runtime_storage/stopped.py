"""Bounded immutable read-only inspection under a continuously pinned diagnostic lease."""

import os
import sqlite3
import time

from jeff.application.diagnostics import DiagnosticDeadlineExceeded
from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_state import DiagnosticLease

from .files import _SIDECARS
from .security import _fault, _guard, _schema


def inspect_stopped_storage(lease: DiagnosticLease, deadline: float) -> int:
    """Only stable sidecar-free rollback storage; no owner thread, writes or migration.

    Reuses actual marker/catalog/ledger policy, not a parallel schema approximation.
    Pathname-open race limits remain the same as the existing storage preflight.
    """

    def no_sidecars(root: int) -> None:
        for name in (*_SIDECARS, "recovery.json"):
            try:
                os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise StorageFault("storage_recovery_required")

    try:
        with lease.data_directory() as root:
            no_sidecars(root)  # Refuse residue before any database body-capable open.
        with lease.data_directory() as root, lease.open_file("database") as fd:
            no_sidecars(root)
            header = os.pread(fd, 100, 0)
            if len(header) != 100 or header[:16] != b"SQLite format 3\0":
                raise StorageFault("storage_unrecognized")
            if header[18:20] != b"\x01\x01":
                raise StorageFault("storage_schema_unsupported")
            lease.check()
            connection = sqlite3.connect(
                lease.layout.database.as_uri() + "?mode=ro&immutable=1",
                uri=True,
                timeout=0,
                isolation_level=None,
            )
            try:
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65536)
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
                _guard(connection)
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA temp_store=MEMORY")
                version = _schema(connection)
                lease.check()
            finally:
                connection.close()
            no_sidecars(root)
            lease.check()
            return version
    except sqlite3.Error as error:
        if time.monotonic() >= deadline:
            raise DiagnosticDeadlineExceeded from None
        raise _fault(error, "storage_unrecognized") from None
    except OSError:
        raise StorageFault("storage_failed") from None
