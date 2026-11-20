"""One checked database open/recovery/migration lifetime; no schema or ordering change."""

import os
import sqlite3
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from jeff.application.history import INT64_MAX
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault
from jeff.application.storage import DATABASE_APPLICATION_ID, StorageFault

from .binding import _method
from .catalog import (
    _ACTIVITY_EVENTS,
    _ACTIVITY_INDEXES,
    _ACTIVITY_META,
    _ACTIVITY_REQUESTS,
    _AUTHORITIES,
    _AUTHORITY_OPS,
    _EXPIRABLE_INDEX,
    _FLOOR_COLUMN,
    _LAUNCH_EVENTS,
    _LAUNCHES,
    _LEDGER,
    _LIFECYCLES,
    _PROJECTS,
    _PYTEST_REPORTS,
    _RECEIPT_INDEX,
    _RECEIPTS,
    _RESULT_PAYLOADS,
    _RESULT_QUARANTINE,
    _RESULTS,
    _REVIEW_OPS,
    _REVIEWS,
    _STOP_PAYLOADS,
    _TOMBSTONES,
)
from .files import _FLAGS, _SIDECARS
from .security import _fault, _guard, _schema

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _open(self: "SQLiteStorage", root: int) -> tuple[sqlite3.Connection, int]:
    self.owner.check()
    for name in _SIDECARS[1:]:
        try:
            os.stat(name, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise StorageFault("storage_recovery_required")
    try:
        os.stat("state.sqlite3-journal", dir_fd=root, follow_symlinks=False)
        recovering = True
    except FileNotFoundError:
        recovering = False
    try:
        os.stat("recovery.json", dir_fd=root, follow_symlinks=False)
        has_permit = True
    except FileNotFoundError:
        has_permit = False
    fd = -1
    journal_fd = -1
    connection: sqlite3.Connection | None = None
    recovery_schema: int | None = None
    try:
        if recovering or has_permit:
            try:
                fd = os.open("state.sqlite3", os.O_RDONLY | _FLAGS, dir_fd=root)
            except FileNotFoundError:
                raise StorageFault("storage_recovery_required") from None
            created = False
        else:
            try:
                fd = os.open(
                    "state.sqlite3",
                    os.O_RDWR | _FLAGS | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root,
                )
                created = True
            except FileExistsError:
                fd = os.open("state.sqlite3", os.O_RDONLY | _FLAGS, dir_fd=root)
                created = False
        self._check(root, fd)
        if recovering:
            try:
                journal_fd = self._authorize_recovery(root, fd)
            except (ControlFault, OSError):
                raise StorageFault("storage_recovery_required") from None
        else:
            try:
                self.owner.retire_storage_recovery(fd)
            except (ControlFault, OSError):
                raise StorageFault("storage_recovery_required") from None
        base = self.owner.store.layout.database.as_uri()
        if not created and not recovering:
            header = os.pread(fd, 100, 0)
            if len(header) != 100 or header[:16] != b"SQLite format 3\0":
                raise StorageFault("storage_unrecognized")
            # File-header read/write versions 1/1 are rollback format. Immutable
            # SQLite deliberately bypasses WAL handling and can report DELETE
            # for a clean WAL file, so its PRAGMA is not this format proof.
            if header[18:20] != b"\x01\x01":
                raise StorageFault("storage_schema_unsupported")
            try:
                probe = sqlite3.connect(
                    base + "?mode=ro&immutable=1", uri=True, isolation_level=None
                )
                try:
                    _guard(probe)
                    _schema(probe)
                finally:
                    probe.close()
            except sqlite3.Error as error:
                raise _fault(error, "storage_unrecognized") from None
            self._check(root, fd)
            self._sidecars(root, preexisting=True)
        if recovering:
            recovery_schema = self.owner.verify_storage_recovery(
                fd, journal_fd
            ).schema_version  # Last authenticated check before writable open.
        connection = sqlite3.connect(
            base + "?mode=rw&cache=private",
            uri=True,
            timeout=DEFAULT_RUNTIME_LIMITS.sqlite_busy_seconds,
            isolation_level=None,
        )
        _guard(connection)
        self._check(root, fd)
        connection.execute("PRAGMA synchronous=FULL")
        if connection.execute("PRAGMA synchronous").fetchone() != (2,):
            raise StorageFault("storage_failed")
        if created:
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise StorageFault("storage_failed")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={DATABASE_APPLICATION_ID}")
            connection.execute(_LEDGER)
            connection.execute("PRAGMA user_version=0")
            connection.execute("COMMIT")
        version = _schema(connection)
        if recovering:
            if version != recovery_schema:
                raise StorageFault("storage_schema_unsupported")
            if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                raise StorageFault("storage_unrecognized")
            self._sidecars(root, preexisting=True)
            self.owner.retire_storage_recovery(fd)
        if version == 0:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_PROJECTS)
            connection.execute(
                "INSERT INTO schema_migrations(version,name) VALUES (?,?)", (1, "registry_v1")
            )
            connection.execute("PRAGMA user_version=1")
            connection.execute("COMMIT")
        if version < 2:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_RECEIPTS)
            connection.execute(_RECEIPT_INDEX)
            connection.execute(
                "INSERT INTO schema_migrations(version,name) VALUES (?,?)",
                (2, "request_receipts_v2"),
            )
            connection.execute("PRAGMA user_version=2")
            connection.execute("COMMIT")
        if version < 3:
            connection.execute("BEGIN IMMEDIATE")
            for sql in (
                _ACTIVITY_META,
                _ACTIVITY_REQUESTS,
                _ACTIVITY_EVENTS,
                *_ACTIVITY_INDEXES.values(),
            ):
                connection.execute(sql)
            connection.execute(
                "INSERT INTO activity_meta VALUES (1,?,?,0,0)",
                (str(uuid4()), time.time_ns() // 1000000),
            )
            connection.execute("INSERT INTO schema_migrations VALUES (3,'activity_v3')")
            connection.execute("PRAGMA user_version=3")
            connection.execute("COMMIT")
        if version < 4:
            connection.execute("BEGIN IMMEDIATE")
            meta = self._activity_meta(connection, legacy=True)
            if meta.retention_generation == INT64_MAX:
                raise StorageFault("storage_schema_unsupported")
            connection.execute(_TOMBSTONES)
            connection.execute(_EXPIRABLE_INDEX)
            connection.execute("ALTER TABLE activity_meta ADD COLUMN " + _FLOOR_COLUMN)
            connection.execute("UPDATE activity_meta SET generation=generation+1")
            connection.execute("INSERT INTO schema_migrations VALUES (4,'retention_v4')")
            connection.execute("PRAGMA user_version=4")
            self._check(root, fd)
            try:
                connection.execute("COMMIT")
                self._check(root, fd)
            except Exception:
                if not connection.in_transaction:
                    raise StorageFault("storage_outcome_unconfirmed") from None
                raise
        if version < 5:
            self._begin_write(connection, root, fd, source_schema=4)
            connection.execute(_REVIEWS)
            connection.execute(_REVIEW_OPS)
            connection.execute("INSERT INTO schema_migrations VALUES (5,'execution_review_v5')")
            connection.execute("PRAGMA user_version=5")
            self._commit_write(connection, fd)
        if version < 6:
            self._begin_write(connection, root, fd, source_schema=5)
            connection.execute(_AUTHORITIES)
            connection.execute(_AUTHORITY_OPS)
            connection.execute("INSERT INTO schema_migrations VALUES (6,'execution_authority_v6')")
            connection.execute("PRAGMA user_version=6")
            self._commit_write(connection, fd)
        if version < 7:
            self._begin_write(connection, root, fd, source_schema=6)
            connection.execute(_LAUNCHES)
            connection.execute(_LAUNCH_EVENTS)
            connection.execute("INSERT INTO schema_migrations VALUES (7,'execution_launch_v7')")
            connection.execute("PRAGMA user_version=7")
            self._commit_write(connection, fd)
        if version < 8:
            self._begin_write(connection, root, fd, source_schema=7)
            connection.execute(_RESULTS)
            connection.execute(_RESULT_PAYLOADS)
            connection.execute(_RESULT_QUARANTINE)
            connection.execute("INSERT INTO schema_migrations VALUES (8,'execution_results_v8')")
            connection.execute("PRAGMA user_version=8")
            self._commit_write(connection, fd)
        if version < 9:
            self._begin_write(connection, root, fd, source_schema=8)
            connection.execute(_LIFECYCLES)
            connection.execute("INSERT INTO schema_migrations VALUES (9,'execution_lifecycle_v9')")
            connection.execute("PRAGMA user_version=9")
            self._commit_write(connection, fd)
        if version < 10:
            self._begin_write(connection, root, fd, source_schema=9)
            connection.execute(_STOP_PAYLOADS)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (10,'execution_stop_payloads_v10')"
            )
            connection.execute("PRAGMA user_version=10")
            self._commit_write(connection, fd)
        if version < 11:
            self._begin_write(connection, root, fd, source_schema=10)
            connection.execute(_PYTEST_REPORTS)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (11,'execution_pytest_reports_v11')"
            )
            connection.execute("PRAGMA user_version=11")
            self._commit_write(connection, fd)
        _schema(connection)
        self._activity_meta(connection)
        if connection.execute(
            "SELECT 1 FROM execution_reviews WHERE state IN ('pending','approved') LIMIT 1"
        ).fetchone():
            self._begin_write(connection, root, fd)
            connection.execute(
                "UPDATE execution_reviews SET state='invalidated' "
                "WHERE state IN ('pending','approved')"
            )
            self._commit_write(connection, fd)
        if connection.execute(
            "SELECT 1 FROM execution_authorities WHERE state IN ('pending','approved') "
            "OR claim_state='claimed' LIMIT 1"
        ).fetchone():
            self._begin_write(connection, root, fd)
            connection.execute(
                "UPDATE execution_authorities SET state='invalidated' "
                "WHERE state IN ('pending','approved')"
            )
            connection.execute(
                "UPDATE execution_authorities SET claim_state='unknown' WHERE claim_state='claimed'"
            )
            self._commit_write(connection, fd)
        if self.instance_id is not None:
            self._reconcile(connection, self.instance_id, root, fd)
        self._check(root, fd)
        return connection, fd
    except BaseException:
        try:
            if connection is not None and connection.in_transaction:
                self._rollback_write(connection, fd)
        finally:
            self._release(connection, fd)
        raise
    finally:
        if journal_fd >= 0:
            os.close(journal_fd)
