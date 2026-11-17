"""SQLite marker/catalog/ledger policy, authorizer and safe error projection."""

import sqlite3

from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS
from jeff.application.storage import (
    DATABASE_APPLICATION_ID,
    DATABASE_SCHEMA,
    StorageFault,
    StorageIssueCode,
)

from .catalog import (
    _ACTIVITY_EVENTS,
    _ACTIVITY_INDEXES,
    _ACTIVITY_META,
    _ACTIVITY_META_V4,
    _ACTIVITY_REQUESTS,
    _AUTHORITIES,
    _AUTHORITY_OPS,
    _EXPIRABLE_INDEX,
    _LAUNCH_EVENTS,
    _LAUNCHES,
    _LEDGER,
    _LIFECYCLES,
    _PROJECTS,
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


def _fault(error: Exception, fallback: StorageIssueCode = "storage_failed") -> StorageFault:
    if isinstance(error, StorageFault):
        return error
    if isinstance(error, sqlite3.Error) and getattr(error, "sqlite_errorcode", 0) & 255 in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    ):
        return StorageFault("storage_busy")
    return StorageFault(fallback)


def _guard(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA trusted_schema=OFF")
    if connection.execute("PRAGMA trusted_schema").fetchone() != (0,):
        raise StorageFault("storage_failed")
    # Builds without the Python extension-loading API cannot enable it through that
    # API. The SQL authorizer denies load_extension regardless of build configuration.
    if hasattr(connection, "enable_load_extension"):
        connection.enable_load_extension(False)

    def authorize(
        action: int, first: str | None, second: str | None, database: str | None, source: str | None
    ) -> int:
        if (
            action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
            or (action == sqlite3.SQLITE_FUNCTION and (second or "").lower() == "load_extension")
            or (action == sqlite3.SQLITE_PRAGMA and (first or "").lower() == "writable_schema")
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    connection.execute(f"PRAGMA busy_timeout={DEFAULT_RUNTIME_LIMITS.sqlite_busy_seconds * 1000}")
    connection.execute("PRAGMA foreign_keys=ON")


def _schema(connection: sqlite3.Connection) -> int:
    if connection.execute("PRAGMA application_id").fetchone() != (DATABASE_APPLICATION_ID,):
        raise StorageFault("storage_unrecognized")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, DATABASE_SCHEMA):
        raise StorageFault("storage_schema_unsupported")
    if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
        raise StorageFault("storage_recovery_required")
    expected = {"schema_migrations": ("table", _LEDGER)}
    if version >= 1:
        expected["projects"] = ("table", _PROJECTS)
    if version >= 2:
        expected["request_receipts"] = ("table", _RECEIPTS)
        expected["receipts_unfinished"] = ("index", _RECEIPT_INDEX)
    if version >= 3:
        expected.update(
            {
                "activity_meta": ("table", _ACTIVITY_META_V4 if version >= 4 else _ACTIVITY_META),
                "activity_requests": ("table", _ACTIVITY_REQUESTS),
                "activity_events": ("table", _ACTIVITY_EVENTS),
            }
        )
        expected.update({name: ("index", sql) for name, sql in _ACTIVITY_INDEXES.items()})
    if version >= 4:
        expected["request_tombstones"] = ("table", _TOMBSTONES)
        expected["receipts_expirable"] = ("index", _EXPIRABLE_INDEX)
    if version >= 5:
        expected["execution_reviews"] = ("table", _REVIEWS)
        expected["execution_operations"] = ("table", _REVIEW_OPS)
    if version >= 6:
        expected["execution_authorities"] = ("table", _AUTHORITIES)
        expected["execution_authority_operations"] = ("table", _AUTHORITY_OPS)
    if version >= 7:
        expected["execution_launches"] = ("table", _LAUNCHES)
        expected["execution_launch_events"] = ("table", _LAUNCH_EVENTS)
    if version >= 8:
        expected["execution_results"] = ("table", _RESULTS)
        expected["execution_result_payloads"] = ("index", _RESULT_PAYLOADS)
        expected["execution_result_quarantine"] = ("index", _RESULT_QUARANTINE)
    if version >= 9:
        expected["execution_lifecycles"] = ("table", _LIFECYCLES)
    if version >= 10:
        expected["execution_stop_payloads"] = ("table", _STOP_PAYLOADS)
    found = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT ?",
        (len(expected) + 1,),
    ).fetchall()

    def normalize(text: object) -> str:
        return " ".join(str(text).upper().split())

    if len(found) != len(expected) or any(
        name not in expected
        or kind != expected[name][0]
        or normalize(sql) != normalize(expected[name][1])
        for kind, name, sql in found
    ):
        raise StorageFault("storage_schema_unsupported")
    ledger = connection.execute(
        "SELECT version,name FROM schema_migrations ORDER BY version LIMIT ?", (version + 1,)
    ).fetchall()
    if ledger != [
        (number, name)
        for number, name in (
            (1, "registry_v1"),
            (2, "request_receipts_v2"),
            (3, "activity_v3"),
            (4, "retention_v4"),
            (5, "execution_review_v5"),
            (6, "execution_authority_v6"),
            (7, "execution_launch_v7"),
            (8, "execution_results_v8"),
            (9, "execution_lifecycle_v9"),
            (10, "execution_stop_payloads_v10"),
        )
        if number <= version
    ]:
        raise StorageFault("storage_schema_unsupported")
    return version
