"""Exact journal authorization and commit/rollback proof retirement on the SQL owner."""

import os
import sqlite3
from typing import TYPE_CHECKING

from jeff.application.runtime import ControlFault
from jeff.application.storage import DATABASE_SCHEMA, StorageFault

from .binding import _method
from .files import _FLAGS
from .security import _schema

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _begin_write(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    root: int,
    fd: int,
    source_schema: int = DATABASE_SCHEMA,
) -> None:
    """Arm the actual DELETE journal before business DML, on the same SQL owner."""
    self._check(root, fd)
    if source_schema not in (4, 5, 6, 7, 8, 9, 10) or _schema(connection) != source_schema:
        raise StorageFault("storage_schema_unsupported")
    try:
        self.owner.store.read_credential()  # Existing authority only, never rekey residue.
        self.owner.retire_storage_recovery(fd)
        connection.execute("BEGIN IMMEDIATE")
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute(f"PRAGMA user_version={source_schema}")  # Real journal first.
        journal = os.open("state.sqlite3-journal", os.O_RDONLY | _FLAGS, dir_fd=root)
        try:
            self.owner.arm_storage_recovery(
                fd,
                journal,
                page_size=page_size,
                original_pages=pages,
                schema_version=source_schema,
            )
        finally:
            os.close(journal)
    except (ControlFault, OSError):
        raise StorageFault("storage_recovery_required") from None


@_method
def _commit_write(self: "SQLiteStorage", connection: sqlite3.Connection, fd: int) -> None:
    """Commit before proof retirement; a later failure is not evidence of rollback."""
    connection.execute("COMMIT")
    try:
        self.owner.retire_storage_recovery(fd)
    except (ControlFault, OSError):
        raise StorageFault("storage_outcome_unconfirmed") from None


@_method
def _rollback_write(self: "SQLiteStorage", connection: sqlite3.Connection, fd: int) -> None:
    """Let SQLite roll back before retiring only the matching recovery permission."""
    connection.execute("ROLLBACK")
    try:
        self.owner.retire_storage_recovery(fd)
    except (ControlFault, OSError):
        raise StorageFault("storage_outcome_unconfirmed") from None
