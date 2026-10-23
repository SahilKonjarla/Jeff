"""Private SQLite registry with one connection-owning thread and bounded declared work.

Python SQLite opens a pathname, not our checked descriptor. Private ancestry plus
before/after identity checks narrow races; this is not a hostile same-user/ACL proof.
"""

import math
import os
import queue
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from jeff.application.history import (
    ACTIVITY_EVENT_LIMIT,
    INT64_MAX,
    ActivityEvent,
    ActivityMeta,
    ActivityPage,
    ActivityRecord,
    ActivityTrace,
    HistoryCursor,
    HistoryItem,
    HistoryPage,
    HistoryRead,
    HistoryScope,
    ReplayScope,
    RetentionPosition,
    StreamCursor,
    StreamPage,
    StreamRead,
    activity_code,
)
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault
from jeff.application.storage import (
    DATABASE_APPLICATION_ID,
    DATABASE_SCHEMA,
    RETIREMENT_BATCH_LIMIT,
    RETIREMENT_INTERVAL_SECONDS,
    STORAGE_PENDING_LIMIT,
    Receipt,
    ReceiptClaim,
    ReceiptClaimResult,
    ReceiptScope,
    ReceiptTransition,
    RetentionProgress,
    RetirementBatch,
    StorageFault,
    StorageIssueCode,
    StorageReadiness,
)
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_settings import load_or_create_settings
from jeff.infrastructure.runtime_state import Ownership

_LEDGER = "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
_PROJECTS = (
    "CREATE TABLE projects (project_id TEXT NOT NULL PRIMARY KEY, "
    "canonical_root TEXT NOT NULL UNIQUE)"
)
_RECEIPTS = (
    "CREATE TABLE request_receipts (request_id TEXT NOT NULL PRIMARY KEY, "
    "owner_instance_id TEXT NOT NULL, operation TEXT NOT NULL "
    "CHECK(operation IN ('init','ask.branch','ask.changes')), "
    "canonical_root TEXT NOT NULL, project_id TEXT, "
    "state TEXT NOT NULL CHECK(state IN ('accepted','running','succeeded','failed','unknown')), "
    "accepted_at_ms INTEGER NOT NULL CHECK(accepted_at_ms>=0), finished_at_ms INTEGER, "
    "outcome_code TEXT CHECK(outcome_code IN "
    "('ok','operation_failed','timeout','interrupted','partial','target_mismatch')), "
    "CHECK(project_id IS NOT NULL OR operation='init'), "
    "CHECK((state IN ('accepted','running') AND finished_at_ms IS NULL "
    "AND outcome_code IS NULL) OR (state IN ('succeeded','failed','unknown') "
    "AND finished_at_ms IS NOT NULL AND outcome_code IS NOT NULL)))"
)
_RECEIPT_INDEX = "CREATE INDEX receipts_unfinished ON request_receipts(state,owner_instance_id)"
_COLUMNS = (
    "request_id,owner_instance_id,operation,canonical_root,project_id,"
    "state,accepted_at_ms,finished_at_ms,outcome_code"
)
_ACTIVITY_META = (
    "CREATE TABLE activity_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "epoch TEXT NOT NULL, "
    "coverage_started_at_ms INTEGER NOT NULL CHECK(coverage_started_at_ms>=0), "
    "high_water INTEGER NOT NULL CHECK(high_water>=0), "
    "generation INTEGER NOT NULL CHECK(generation>=0))"
)
_FLOOR_COLUMN = "replay_floor INTEGER NOT NULL DEFAULT 0 CHECK(replay_floor>=0)"
_ACTIVITY_META_V4 = _ACTIVITY_META[:-1] + ", " + _FLOOR_COLUMN + ")"
_TOMBSTONES = "CREATE TABLE request_tombstones (request_id TEXT NOT NULL PRIMARY KEY)"
_EXPIRABLE_INDEX = (
    "CREATE INDEX receipts_expirable ON request_receipts(finished_at_ms,request_id) "
    "WHERE state IN ('succeeded','failed')"
)
_ACTIVITY_REQUESTS = (
    "CREATE TABLE activity_requests ("
    "request_id TEXT PRIMARY KEY REFERENCES request_receipts(request_id), "
    "canonical_root TEXT NOT NULL, project_id TEXT, accepted_sequence INTEGER NOT NULL UNIQUE)"
)
_ACTIVITY_EVENTS = (
    "CREATE TABLE activity_events (event_id INTEGER PRIMARY KEY, "
    "request_id TEXT NOT NULL REFERENCES request_receipts(request_id), "
    "owner_instance_id TEXT NOT NULL, "
    "canonical_root TEXT NOT NULL, project_id TEXT, scope_project_id TEXT, kind TEXT NOT NULL, "
    "recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms>=0), code TEXT, git_category TEXT, "
    "elapsed_ms INTEGER, output_bytes INTEGER, git_started INTEGER CHECK(git_started IN (0,1)), "
    "registration_created INTEGER CHECK(registration_created IN (0,1)))"
)
_ACTIVITY_INDEXES = {
    "activity_trace": "CREATE INDEX activity_trace ON activity_events(request_id,event_id)",
    "activity_replay": (
        "CREATE INDEX activity_replay ON activity_events(scope_project_id,canonical_root,event_id)"
    ),
    "activity_history": (
        "CREATE INDEX activity_history "
        "ON activity_requests(project_id,canonical_root,accepted_sequence)"
    ),
}
_EVENT_COLUMNS = (
    "event_id,request_id,owner_instance_id,canonical_root,project_id,kind,recorded_at_ms,"
    "code,git_category,elapsed_ms,output_bytes,git_started,registration_created"
)
_SIDECARS = ("state.sqlite3-journal", "state.sqlite3-wal", "state.sqlite3-shm")
_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink


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
    if version not in (0, 1, 2, 3, DATABASE_SCHEMA):
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
        )
        if number <= version
    ]:
        raise StorageFault("storage_schema_unsupported")
    return version


@dataclass
class _Work:
    operation: Literal[
        "index",
        "lookup",
        "claim",
        "finish",
        "inspect",
        "root_claimed",
        "observe",
        "trace",
        "replay",
        "debug",
        "history",
        "stream",
        "retire",
    ]
    payload: (
        Project
        | ReceiptClaim
        | ReceiptTransition
        | ReceiptScope
        | Path
        | ActivityRecord
        | ReplayScope
        | HistoryScope
        | None
    )
    deadline: float
    result: Future[object]


class SQLiteStorage:
    """Start/close belong to explicit lifetime ownership; registry calls block callers,
    never the control loop. No SQL/connection/cursor is exposed through this interface.
    """

    def __init__(
        self,
        owner: Ownership,
        *,
        instance_id: UUID | None = None,
        commit_hint: Callable[[], None] | None = None,
        retention_clock: Callable[[], int] | None = None,
        retention_monotonic: Callable[[], float] | None = None,
        scheduled_retention: bool = False,
    ) -> None:
        self._retention_monotonic = retention_monotonic or time.monotonic
        self._retention_progress = RetentionProgress(enabled=scheduled_retention)
        self._retention_position: RetentionPosition | None = None
        self._retention_clock = retention_clock or (lambda: time.time_ns() // 1000000)
        self._retention_days: int | None = None
        self._retention_mono_high: float | None = None
        self._retention_wall_high: int | None = None
        self._retention_clock_deferred = False
        self._commit_hint = commit_hint
        self._activity_dirty = False
        self._notification_failed = threading.Event()
        self.owner = owner
        self.instance_id = instance_id
        self._mutex = threading.Lock()
        self._state = StorageReadiness()
        self._initialized = threading.Event()
        self._closing = threading.Event()
        self._queue: queue.Queue[_Work] = queue.Queue(STORAGE_PENDING_LIMIT)
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._close_fault: StorageFault | None = None

    @property
    def notification_healthy(self) -> bool:
        return not self._notification_failed.is_set()

    def pause_retention(self) -> None:
        """Seal maintenance admission, not active SQL or ordinary action drainage."""
        with self._mutex:
            self._retention_progress = self._retention_progress.model_copy(
                update={
                    "paused": True,
                    "next_attempt_at": None,
                }
            )

    def retention_position(self) -> RetentionPosition | None:
        """Mutex-only committed epoch/generation/floor; no SQL or diagnostic writes."""
        with self._mutex:
            return self._retention_position

    def _publish_retention(self, meta: ActivityMeta) -> None:
        position = RetentionPosition(
            database_epoch=meta.database_epoch,
            retention_generation=meta.retention_generation,
            replay_floor=meta.replay_floor,
        )
        with self._mutex:
            self._retention_position = position

    def retention_snapshot(self) -> RetentionProgress:
        with self._mutex:
            return self._retention_progress

    def snapshot(self) -> StorageReadiness:
        with self._mutex:
            return self._state

    def start(self) -> None:
        with self._mutex:
            if self._thread is not None or self._closing.is_set():
                raise StorageFault("storage_closing")
            self._state = StorageReadiness(state="initializing")
            self._thread = threading.Thread(
                target=self._run, name="jeff-storage-owner", daemon=False
            )
            self._thread.start()

    def wait_ready(self, timeout: float) -> StorageReadiness:
        if not self._initialized.wait(timeout):
            raise StorageFault("storage_outcome_unconfirmed")
        return self.snapshot()

    @property
    def closed(self) -> bool:
        return self._thread is None or not self._thread.is_alive()

    def request_close(self) -> None:
        with self._mutex:
            self._accepting = False
            self._closing.set()
            self._retention_progress = self._retention_progress.model_copy(
                update={
                    "paused": True,
                    "next_attempt_at": None,
                }
            )
            if self._state.state != "failed":
                self._state = StorageReadiness(state="closing")

    def close(self, timeout: float = DEFAULT_RUNTIME_LIMITS.stop_seconds) -> None:
        self.request_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise StorageFault("storage_outcome_unconfirmed")
        if self._close_fault is not None:
            raise self._close_fault

    def index(self, project: Project, *, deadline: float | None = None) -> tuple[Project, bool]:
        return project, cast(bool, self._submit("index", project, deadline))

    def lookup(self, project: Project, *, deadline: float | None = None) -> Project | None:
        return project if self._submit("lookup", project, deadline) else None

    def root_claimed(self, root: Path, *, deadline: float | None = None) -> bool:
        if not root.is_absolute() or "\0" in str(root):
            raise StorageFault("registry_conflict")
        return cast(bool, self._submit("root_claimed", root, deadline))

    def claim(self, claim: ReceiptClaim, *, deadline: float) -> ReceiptClaimResult:
        return cast(ReceiptClaimResult, self._submit("claim", claim, deadline))

    def finish(self, transition: ReceiptTransition, *, deadline: float) -> Receipt:
        return cast(Receipt, self._submit("finish", transition, deadline))

    def inspect(self, scope: ReceiptScope, *, deadline: float) -> Receipt | None:
        return cast(Receipt | None, self._submit("inspect", scope, deadline))

    def observe(self, record: ActivityRecord, *, deadline: float) -> ActivityEvent:
        return cast(ActivityEvent, self._submit("observe", record, deadline))

    def trace(
        self, scope: ReceiptScope, *, deadline: float
    ) -> tuple[Receipt | None, ActivityTrace | None]:
        return cast(
            tuple[Receipt | None, ActivityTrace | None], self._submit("trace", scope, deadline)
        )

    def replay(self, scope: ReplayScope, *, deadline: float) -> ActivityPage:
        return cast(ActivityPage, self._submit("replay", scope, deadline))

    def debug(self, scope: HistoryScope, *, deadline: float) -> tuple[Receipt, ActivityTrace]:
        return cast(tuple[Receipt, ActivityTrace], self._submit("debug", scope, deadline))

    def history(self, scope: HistoryRead, *, deadline: float) -> HistoryPage:
        return cast(HistoryPage, self._submit("history", scope, deadline))

    def stream_page(self, scope: StreamRead, *, deadline: float) -> StreamPage:
        return cast(StreamPage, self._submit("stream", scope, deadline))

    def retire(self, *, deadline: float) -> RetirementBatch:
        """Installation-internal bounded maintenance; no cutoff/SQL or wire operation."""
        return cast(RetirementBatch, self._submit("retire", None, deadline))

    def _submit(
        self,
        operation: Literal[
            "index",
            "lookup",
            "claim",
            "finish",
            "inspect",
            "root_claimed",
            "observe",
            "trace",
            "replay",
            "debug",
            "history",
            "stream",
            "retire",
        ],
        payload: Project
        | ReceiptClaim
        | ReceiptTransition
        | ReceiptScope
        | Path
        | ActivityRecord
        | ReplayScope
        | HistoryScope
        | None,
        deadline: float | None,
    ) -> object:
        if isinstance(payload, Project) and (
            not isinstance(payload.project_id, UUID)
            or not payload.root.is_absolute()
            or "\0" in str(payload.root)
        ):
            raise StorageFault("registry_conflict")
        deadline = min(
            deadline if deadline is not None else float("inf"),
            time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds,
        )
        if deadline <= time.monotonic():
            raise StorageFault("storage_busy")
        result: Future[object] = Future()
        with self._mutex:
            if not self._accepting:
                raise StorageFault("storage_closing")
            try:
                self._queue.put_nowait(
                    _Work(
                        operation,
                        payload,
                        deadline,
                        result,
                    )
                )
            except queue.Full:
                raise StorageFault("storage_busy") from None
        try:
            return result.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeout:
            # Only queued work can be cancelled safely; running SQL keeps its slot.
            raise StorageFault(
                "storage_busy" if result.cancel() else "storage_outcome_unconfirmed"
            ) from None

    def _sidecars(self, root: int, *, preexisting: bool) -> None:
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

    def _check(self, root: int, fd: int) -> None:
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

    def _open(self, root: int) -> tuple[sqlite3.Connection, int]:
        self.owner.check()
        self._sidecars(root, preexisting=True)
        fd = -1
        connection: sqlite3.Connection | None = None
        try:
            try:
                fd = os.open(
                    "state.sqlite3", os.O_RDWR | _FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root
                )
                created = True
            except FileExistsError:
                fd = os.open("state.sqlite3", os.O_RDONLY | _FLAGS, dir_fd=root)
                created = False
            self._check(root, fd)
            base = self.owner.store.layout.database.as_uri()
            if not created:
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
            _schema(connection)
            self._activity_meta(connection)
            if self.instance_id is not None:
                self._reconcile(connection, self.instance_id)
            self._check(root, fd)
            return connection, fd
        except BaseException:
            try:
                if connection is not None and connection.in_transaction:
                    connection.execute("ROLLBACK")
            finally:
                self._release(connection, fd)
            raise

    def _execute(self, connection: sqlite3.Connection, work: _Work, root: int, fd: int) -> object:
        self._check(root, fd)
        if work.operation == "retire":
            return self._retire(connection, root, fd)
        if work.operation == "stream":
            assert isinstance(work.payload, StreamRead)
            return self._stream_page(connection, work.payload, root, fd)
        if work.operation == "history":
            assert isinstance(work.payload, HistoryRead)
            return self._history(connection, work.payload, root, fd)
        if work.operation in ("observe", "trace", "replay", "debug"):
            return self._activity_work(connection, work, root, fd)
        if work.operation == "root_claimed":
            assert isinstance(work.payload, Path)
            return (
                connection.execute(
                    "SELECT 1 FROM projects WHERE canonical_root=?", (str(work.payload),)
                ).fetchone()
                is not None
            )
        if work.operation not in ("index", "lookup"):
            return self._receipt_work(connection, work, root, fd)
        assert isinstance(work.payload, Project)
        project_id, path = str(work.payload.project_id), str(work.payload.root)
        writing = work.operation == "index"
        if writing:
            connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT project_id,canonical_root FROM projects WHERE project_id=? OR canonical_root=?",
            (project_id, path),
        ).fetchall()
        if rows and rows != [(project_id, path)]:
            raise StorageFault("registry_conflict")
        created = not rows
        if writing and created:
            connection.execute(
                "INSERT INTO projects(project_id,canonical_root) VALUES (?,?)", (project_id, path)
            )
        self._check(root, fd)
        try:
            if writing:
                connection.execute("COMMIT")
            self._check(root, fd)
        except (sqlite3.Error, StorageFault, ControlFault, OSError):
            if writing and not connection.in_transaction:
                raise StorageFault("storage_outcome_unconfirmed") from None
            raise
        return created if writing else bool(rows)

    @staticmethod
    def _receipt(row: tuple[object, ...]) -> Receipt:
        return Receipt.model_validate(dict(zip(_COLUMNS.split(","), row, strict=True)))

    @staticmethod
    def _activity_meta(connection: sqlite3.Connection, *, legacy: bool = False) -> ActivityMeta:
        fields = "epoch,coverage_started_at_ms,high_water,generation"
        row = connection.execute(
            f"SELECT {fields}{'' if legacy else ',replay_floor'} "
            "FROM activity_meta WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise StorageFault("storage_unrecognized")
        return ActivityMeta(
            database_epoch=row[0],
            coverage_started_at_ms=row[1],
            high_water=row[2],
            retention_generation=row[3],
            replay_floor=0 if legacy else row[4],
        )

    @staticmethod
    def _activity_event(row: tuple[object, ...]) -> ActivityEvent:
        (
            sequence,
            request,
            owner,
            root,
            project,
            kind,
            at,
            code,
            category,
            elapsed,
            size,
            started,
            created,
        ) = row
        return ActivityEvent.model_validate(
            {
                "sequence": sequence,
                "request_id": request,
                "owner_instance_id": owner,
                "canonical_root": root,
                "project_id": project,
                "kind": kind,
                "recorded_at_ms": at,
                "code": code,
                "git": None
                if category is None
                else {
                    "category": category,
                    "process_started": bool(started),
                    "code": code,
                    "elapsed_ms": elapsed,
                    "output_bytes": size,
                },
                "registration_created": None if created is None else bool(created),
            }
        )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        receipt: Receipt,
        kind: str,
        *,
        code: str | None = None,
        record: ActivityRecord | None = None,
        fresh: bool = False,
    ) -> ActivityEvent | None:
        if (
            not fresh
            and connection.execute(
                "SELECT 1 FROM activity_requests WHERE request_id=?", (str(receipt.request_id),)
            ).fetchone()
            is None
        ):
            return None  # Existing receipts are legacy metadata, never invented traces.
        count = int(
            connection.execute(
                "SELECT count(*) FROM activity_events WHERE request_id=?",
                (str(receipt.request_id),),
            ).fetchone()[0]
        )
        terminal = kind in (
            "request.succeeded",
            "request.failed",
            "request.unknown",
            "request.interrupted",
        )
        if count >= (ACTIVITY_EVENT_LIMIT if terminal else ACTIVITY_EVENT_LIMIT - 2):
            raise StorageFault("activity_budget_exceeded")
        meta = self._activity_meta(connection)
        git = record.git if record is not None else None
        event = ActivityEvent.model_validate(
            {
                "sequence": meta.high_water + 1,
                "request_id": receipt.request_id,
                "owner_instance_id": receipt.owner_instance_id,
                "canonical_root": receipt.canonical_root,
                "project_id": receipt.project_id,
                "kind": kind,
                "recorded_at_ms": time.time_ns() // 1000000,
                "code": git.code
                if git is not None
                else activity_code(code)
                if code is not None
                else None,
                "git": git,
                "registration_created": record.registration_created if record is not None else None,
            }
        )
        connection.execute(
            "INSERT INTO activity_events(event_id,request_id,owner_instance_id,canonical_root,"
            "project_id,scope_project_id,kind,recorded_at_ms,code,git_category,elapsed_ms,output_bytes,"
            "git_started,registration_created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.sequence,
                str(event.request_id),
                str(event.owner_instance_id),
                str(event.canonical_root),
                str(event.project_id) if event.project_id else None,
                str(event.project_id) if event.project_id else None,
                event.kind,
                event.recorded_at_ms,
                event.code,
                git.category if git else None,
                git.elapsed_ms if git else None,
                git.output_bytes if git else None,
                int(git.process_started) if git else None,
                int(event.registration_created) if event.registration_created is not None else None,
            ),
        )
        connection.execute(
            "UPDATE activity_meta SET high_water=? WHERE singleton=1", (event.sequence,)
        )
        self._activity_dirty = True
        return event

    @staticmethod
    def _scoped_receipt(connection: sqlite3.Connection, scope: ReceiptScope) -> Receipt | None:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts WHERE request_id=?", (str(scope.request_id),)
        ).fetchone()
        receipt = SQLiteStorage._receipt(row) if row is not None else None
        if receipt is not None and (
            receipt.canonical_root != scope.canonical_root
            or (
                receipt.project_id != scope.project_id
                and not (
                    scope.allow_unbound_init
                    and receipt.operation == "init"
                    and receipt.project_id is None
                )
            )
        ):
            raise StorageFault("receipt_scope_mismatch")
        return receipt

    def _stream_page(
        self, connection: sqlite3.Connection, scope: StreamRead, root: int, fd: int
    ) -> StreamPage:
        meta = self._activity_meta(connection)
        after = meta.high_water
        events: tuple[ActivityEvent, ...] = ()
        more = False
        if scope.cursor is not None:
            try:
                cursor = StreamCursor.decode(scope.cursor)
            except ValueError:
                raise StorageFault("activity_invalid_cursor") from None
            if (
                cursor.project_id != scope.project_id
                or cursor.canonical_root != scope.canonical_root
                or cursor.database_epoch != meta.database_epoch
                or cursor.retention_generation != meta.retention_generation
                or cursor.after > meta.high_water
                or cursor.after < meta.replay_floor
            ):
                raise StorageFault("activity_invalid_cursor")
            after = cursor.after
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM activity_events WHERE scope_project_id=? "
                "AND canonical_root=? AND project_id=? AND event_id>? AND event_id<=? "
                "ORDER BY event_id LIMIT ?",
                (
                    str(scope.project_id),
                    str(scope.canonical_root),
                    str(scope.project_id),
                    after,
                    meta.high_water,
                    scope.limit + 1,
                ),
            ).fetchall()
            events = tuple(self._activity_event(row) for row in rows[: scope.limit])
            more = len(rows) > scope.limit
        self._check(root, fd)
        return StreamPage(
            **meta.model_dump(),
            project_id=scope.project_id,
            canonical_root=scope.canonical_root,
            start_after=after,
            scanned_through=events[-1].sequence if more else meta.high_water,
            events=events,
            has_more=more,
        )

    def _history(
        self, connection: sqlite3.Connection, scope: HistoryRead, root: int, fd: int
    ) -> HistoryPage:
        meta = self._activity_meta(connection)
        ceiling, before = meta.high_water, meta.high_water
        seek = "<=" if scope.cursor is None else "<"
        if scope.cursor is not None:
            try:
                cursor = HistoryCursor.decode(scope.cursor)
            except ValueError:
                raise StorageFault("activity_invalid_cursor") from None
            if (
                cursor.project_id != scope.project_id
                or cursor.canonical_root != scope.canonical_root
                or cursor.database_epoch != meta.database_epoch
                or cursor.retention_generation != meta.retention_generation
                or cursor.snapshot_high_water > meta.high_water
            ):
                raise StorageFault("activity_invalid_cursor")
            ceiling, before = cursor.snapshot_high_water, cursor.before
            if (
                connection.execute(
                    "SELECT 1 FROM activity_requests WHERE project_id=? AND canonical_root=? "
                    "AND accepted_sequence=?",
                    (str(scope.project_id), str(scope.canonical_root), before),
                ).fetchone()
                is None
            ):
                raise StorageFault("activity_invalid_cursor")
        columns = ",".join("r." + name for name in _COLUMNS.split(","))
        rows = connection.execute(
            f"SELECT {columns},a.accepted_sequence FROM activity_requests a "
            "JOIN request_receipts r ON r.request_id=a.request_id "
            "WHERE a.project_id=? AND a.canonical_root=? AND a.accepted_sequence<=? "
            f"AND a.accepted_sequence{seek}? "
            "AND NOT EXISTS (SELECT 1 FROM activity_events e WHERE e.request_id=a.request_id "
            "AND e.kind='identity.established' AND e.event_id>?) "
            "ORDER BY a.accepted_sequence DESC LIMIT ?",
            (
                str(scope.project_id),
                str(scope.canonical_root),
                ceiling,
                before,
                ceiling,
                scope.limit + 1,
            ),
        ).fetchall()
        items = tuple(
            HistoryItem(
                accepted_sequence=row[-1],
                **self._receipt(row[:-1]).model_dump(exclude={"owner_instance_id"}),
            )
            for row in rows[: scope.limit]
        )
        next_cursor = None
        if len(rows) > scope.limit:
            next_cursor = HistoryCursor(
                project_id=scope.project_id,
                canonical_root=scope.canonical_root,
                database_epoch=meta.database_epoch,
                retention_generation=meta.retention_generation,
                snapshot_high_water=ceiling,
                before=items[-1].accepted_sequence,
            ).encode()
        self._check(root, fd)
        return HistoryPage(
            **meta.model_dump(), snapshot_high_water=ceiling, items=items, next_cursor=next_cursor
        )

    def _activity_work(
        self, connection: sqlite3.Connection, work: _Work, root: int, fd: int
    ) -> object:
        payload = work.payload
        if work.operation == "debug":
            assert isinstance(payload, HistoryScope)
            row = connection.execute(
                "SELECT request_id FROM activity_requests WHERE project_id=? "
                "AND canonical_root=? ORDER BY accepted_sequence DESC LIMIT 1",
                (str(payload.project_id), str(payload.canonical_root)),
            ).fetchone()
            if row is None:
                raise StorageFault("no_history")
            payload = ReceiptScope(
                request_id=row[0],
                canonical_root=payload.canonical_root,
                project_id=payload.project_id,
            )
        if work.operation in ("trace", "debug"):
            assert isinstance(payload, ReceiptScope)
            receipt = self._scoped_receipt(connection, payload)
            if receipt is None:
                return None, None
            known = (
                connection.execute(
                    "SELECT 1 FROM activity_requests WHERE request_id=?", (str(payload.request_id),)
                ).fetchone()
                is not None
            )
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM activity_events "
                "WHERE request_id=? ORDER BY event_id LIMIT 33",
                (str(payload.request_id),),
            ).fetchall()
            trace = ActivityTrace(
                **self._activity_meta(connection).model_dump(),
                legacy_metadata_only=not known,
                events=tuple(self._activity_event(row) for row in rows),
            )
            self._check(root, fd)
            return receipt, trace
        if work.operation == "replay":
            assert isinstance(payload, ReplayScope)
            meta = self._activity_meta(connection)
            if (
                payload.after < meta.replay_floor
                or payload.after > meta.high_water
                or (
                    payload.expected_epoch is not None
                    and payload.expected_epoch != meta.database_epoch
                )
            ):
                raise StorageFault("activity_invalid_cursor")
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM activity_events WHERE scope_project_id=? "
                "AND canonical_root=? AND event_id>? AND event_id<=? ORDER BY event_id LIMIT ?",
                (
                    str(payload.project_id),
                    str(payload.canonical_root),
                    payload.after,
                    meta.high_water,
                    payload.limit + 1,
                ),
            ).fetchall()
            events = tuple(self._activity_event(row) for row in rows[: payload.limit])
            self._check(root, fd)
            return ActivityPage(
                **meta.model_dump(),
                events=events,
                has_more=len(rows) > payload.limit,
                next_after=events[-1].sequence if events else payload.after,
            )
        assert isinstance(payload, ActivityRecord)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts WHERE request_id=?",
            (str(payload.request_id),),
        ).fetchone()
        receipt = self._receipt(row) if row is not None else None
        if (
            receipt is None
            or receipt.owner_instance_id != payload.owner_instance_id
            or receipt.state != "running"
            or (self.instance_id is not None and payload.owner_instance_id != self.instance_id)
        ):
            raise StorageFault("receipt_conflict")
        event = self._append_event(
            connection,
            receipt,
            "git.finished" if payload.git is not None else "registration.observed",
            record=payload,
        )
        if event is None:
            raise StorageFault("receipt_conflict")
        self._check(root, fd)
        try:
            connection.execute("COMMIT")
            self._check(root, fd)
        except Exception:
            if not connection.in_transaction:
                raise StorageFault("storage_outcome_unconfirmed") from None
            raise
        return event

    def _retire(self, connection: sqlite3.Connection, root: int, fd: int) -> RetirementBatch:
        with self._mutex:
            if self._retention_progress.paused or self._closing.is_set():
                raise StorageFault("storage_closing")
        assert self._retention_days is not None
        now = self._sample_retention_wall()
        self._retention_clock_deferred = now is None
        if now is None:
            raise StorageFault("storage_busy")  # Defer; never clamp to a stale larger cutoff.
        cutoff = max(0, now - self._retention_days * 86400000)
        connection.execute("BEGIN IMMEDIATE")
        meta = self._activity_meta(connection)
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts "
            "WHERE state IN ('succeeded','failed') AND finished_at_ms<? "
            "ORDER BY finished_at_ms,request_id LIMIT ?",
            (cutoff, RETIREMENT_BATCH_LIMIT + 1),
        ).fetchall()
        selected = rows[:RETIREMENT_BATCH_LIMIT]
        if selected and meta.retention_generation == INT64_MAX:
            raise StorageFault("storage_failed")
        floor = meta.replay_floor
        for row in selected:
            receipt = self._receipt(row)
            request_id = str(receipt.request_id)
            if row[0] != request_id:
                raise StorageFault("storage_unrecognized")
            membership = connection.execute(
                "SELECT canonical_root,project_id,accepted_sequence FROM activity_requests "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()
            event_rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM activity_events WHERE request_id=? "
                "ORDER BY event_id LIMIT ?",
                (request_id, ACTIVITY_EVENT_LIMIT + 1),
            ).fetchall()
            trace = ActivityTrace(
                **meta.model_dump(),
                legacy_metadata_only=membership is None,
                events=tuple(self._activity_event(event) for event in event_rows),
            )
            if membership is not None and membership != (
                str(receipt.canonical_root),
                str(receipt.project_id) if receipt.project_id else None,
                trace.events[0].sequence,
            ):
                raise StorageFault("storage_unrecognized")
            if any(
                event.request_id != receipt.request_id
                or event.owner_instance_id != receipt.owner_instance_id
                or event.canonical_root != receipt.canonical_root
                for event in trace.events
            ):
                raise StorageFault("storage_unrecognized")
            floor = max(floor, trace.events[-1].sequence if trace.events else 0)
            connection.execute("INSERT INTO request_tombstones VALUES (?)", (request_id,))
            connection.execute("DELETE FROM activity_events WHERE request_id=?", (request_id,))
            connection.execute("DELETE FROM activity_requests WHERE request_id=?", (request_id,))
            connection.execute("DELETE FROM request_receipts WHERE request_id=?", (request_id,))
        if selected:
            connection.execute(
                "UPDATE activity_meta SET generation=generation+1,replay_floor=? WHERE singleton=1",
                (floor,),
            )
        result = RetirementBatch(
            **self._activity_meta(connection).model_dump(),
            retired_count=len(selected),
            has_more=len(rows) > RETIREMENT_BATCH_LIMIT,
        )
        self._check(root, fd)
        try:
            connection.execute("COMMIT")
            self._check(root, fd)
        except Exception:
            if not connection.in_transaction:
                raise StorageFault("storage_outcome_unconfirmed") from None
            raise
        self._publish_retention(result)  # Checked COMMIT precedes cache, then caller's hint.
        self._activity_dirty = bool(selected)
        return result

    def _sample_retention_wall(self) -> int | None:
        try:
            now = self._retention_clock()
        except Exception:
            return None
        if (
            type(now) is not int
            or not 0 <= now <= INT64_MAX
            or (self._retention_wall_high is not None and now < self._retention_wall_high)
        ):
            return None
        self._retention_wall_high = now
        return now

    def _retention_now(self) -> float | None:
        try:
            value = self._retention_monotonic()
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
                or not math.isfinite(value + RETIREMENT_INTERVAL_SECONDS)
                or value + RETIREMENT_INTERVAL_SECONDS - value < RETIREMENT_INTERVAL_SECONDS
                or (self._retention_mono_high is not None and value < self._retention_mono_high)
            ):
                return None
            self._retention_mono_high = float(value)
            return float(value)
        except Exception:
            return None

    def _retention_end(
        self, *, batch: RetirementBatch | None = None, fault: StorageFault | None = None
    ) -> None:
        completed = self._retention_now()
        with self._mutex:
            progress = self._retention_progress
            self._retention_progress = progress.model_copy(
                update={
                    "active": False,
                    "next_attempt_at": completed + RETIREMENT_INTERVAL_SECONDS
                    if completed is not None
                    and progress.enabled
                    and not progress.paused
                    and not self._closing.is_set()
                    else None,
                    "last_outcome": "committed"
                    if fault is None
                    else "clock_deferred"
                    if self._retention_clock_deferred
                    else "busy"
                    if fault.code in ("storage_busy", "storage_closing")
                    else "failed",
                    "last_batch": batch if batch is not None else progress.last_batch,
                    "error": fault.code if fault is not None else None,
                }
            )

    def _maintain_due(self, connection: sqlite3.Connection, root: int, fd: int) -> None:
        now = self._retention_now()
        with self._mutex:
            progress = self._retention_progress
            if (
                not progress.enabled
                or progress.paused
                or self._closing.is_set()
                or self._state.state != "ready"
                or now is None
            ):
                return
            if progress.next_attempt_at is None:
                self._retention_progress = progress.model_copy(
                    update={
                        "next_attempt_at": now + RETIREMENT_INTERVAL_SECONDS,
                    }
                )
                return
            if now < progress.next_attempt_at:
                return
            self._retention_progress = progress.model_copy(
                update={
                    "active": True,
                    "attempts": progress.attempts + 1,
                    "next_attempt_at": None,
                }
            )
        self._activity_dirty = False
        self._retention_clock_deferred = False
        try:
            batch = self._retire(connection, root, fd)
            if self._activity_dirty and self._commit_hint is not None:
                try:
                    self._commit_hint()
                except Exception:
                    self._notification_failed.set()
            self._retention_end(batch=batch)
        except Exception as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except sqlite3.Error:
                fault = StorageFault("storage_outcome_unconfirmed")
            else:
                fault = _fault(error)
            if fault.code not in ("storage_busy", "storage_closing"):
                self._failed(fault)
                self._closing.set()
            self._retention_end(fault=fault)

    def _reconcile(self, connection: sqlite3.Connection, instance: UUID) -> None:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.start_seconds
        while True:
            if time.monotonic() >= deadline:
                raise StorageFault("storage_busy")
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM request_receipts WHERE state IN ('accepted','running') "
                "AND owner_instance_id<>? LIMIT 100",
                (str(instance),),
            ).fetchall()
            if not rows:
                connection.execute("COMMIT")
                return
            for row in rows:
                receipt = self._receipt(row)
                connection.execute(
                    "UPDATE request_receipts SET state='unknown',finished_at_ms=?,"
                    "outcome_code='interrupted' WHERE request_id=?",
                    (time.time_ns() // 1000000, str(receipt.request_id)),
                )
                self._append_event(connection, receipt, "request.interrupted", code="interrupted")
            connection.execute("COMMIT")

    def _receipt_work(
        self, connection: sqlite3.Connection, work: _Work, root: int, fd: int
    ) -> object:
        payload = work.payload
        assert isinstance(payload, (ReceiptClaim, ReceiptTransition, ReceiptScope))
        writing = work.operation != "inspect"
        if writing:
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts WHERE request_id=?",
            (str(payload.request_id),),
        ).fetchone()
        receipt = self._receipt(row) if row is not None else None
        result: object
        if isinstance(payload, ReceiptClaim):
            if receipt is None and (
                self.instance_id is not None and payload.owner_instance_id != self.instance_id
            ):
                raise StorageFault("receipt_conflict")
            retired = connection.execute(
                "SELECT 1 FROM request_tombstones WHERE request_id=?", (str(payload.request_id),)
            ).fetchone()
            if retired is not None:
                raise StorageFault(
                    "storage_unrecognized" if receipt is not None else "request_retired"
                )
            if receipt is not None:
                if (
                    receipt.operation != payload.operation
                    or receipt.canonical_root != payload.canonical_root
                    or (
                        receipt.project_id != payload.project_id
                        and not (receipt.operation == "init" and receipt.project_id is None)
                    )
                ):
                    raise StorageFault("receipt_conflict")
                result = ReceiptClaimResult(created=False, receipt=receipt)
            else:
                if self.instance_id is not None and payload.owner_instance_id != self.instance_id:
                    raise StorageFault("receipt_conflict")
                receipt = Receipt(
                    **payload.model_dump(),
                    state="accepted",
                    accepted_at_ms=time.time_ns() // 1000000,
                )
                connection.execute(
                    f"INSERT INTO request_receipts({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(receipt.request_id),
                        str(receipt.owner_instance_id),
                        receipt.operation,
                        str(receipt.canonical_root),
                        str(receipt.project_id) if receipt.project_id is not None else None,
                        receipt.state,
                        receipt.accepted_at_ms,
                        None,
                        None,
                    ),
                )
                event = self._append_event(connection, receipt, "request.accepted", fresh=True)
                assert event is not None
                connection.execute(
                    "INSERT INTO activity_requests VALUES (?,?,?,?)",
                    (
                        str(receipt.request_id),
                        str(receipt.canonical_root),
                        str(receipt.project_id) if receipt.project_id else None,
                        event.sequence,
                    ),
                )
                result = ReceiptClaimResult(created=True, receipt=receipt)
        elif isinstance(payload, ReceiptScope):
            if receipt is not None and (
                receipt.canonical_root != payload.canonical_root
                or (
                    receipt.project_id != payload.project_id
                    and not (
                        payload.allow_unbound_init
                        and receipt.operation == "init"
                        and receipt.project_id is None
                    )
                )
            ):
                raise StorageFault("receipt_scope_mismatch")
            result = receipt
        else:
            associating = (
                receipt is not None
                and receipt.operation == "init"
                and receipt.project_id is None
                and receipt.state == "running"
                and payload.state == "running"
                and payload.project_id is not None
            )
            if (
                receipt is None
                or receipt.owner_instance_id != payload.owner_instance_id
                or (self.instance_id is not None and payload.owner_instance_id != self.instance_id)
                or receipt.state not in ("accepted", "running")
                or (payload.state == "running" and receipt.state != "accepted" and not associating)
                or (payload.project_id is not None and not associating)
            ):
                raise StorageFault("receipt_conflict")
            finished = None if payload.state == "running" else time.time_ns() // 1000000
            project_id = payload.project_id if associating else receipt.project_id
            connection.execute(
                "UPDATE request_receipts SET state=?,finished_at_ms=?,outcome_code=?,project_id=? "
                "WHERE request_id=?",
                (
                    payload.state,
                    finished,
                    payload.outcome_code,
                    str(project_id) if project_id is not None else None,
                    str(payload.request_id),
                ),
            )
            result = Receipt.model_validate(
                {
                    **receipt.model_dump(),
                    "state": payload.state,
                    "project_id": project_id,
                    "finished_at_ms": finished,
                    "outcome_code": payload.outcome_code,
                }
            )
        if isinstance(payload, ReceiptTransition):
            assert isinstance(result, Receipt)
            if associating:
                connection.execute(
                    "UPDATE activity_requests SET project_id=? WHERE request_id=?",
                    (str(result.project_id), str(result.request_id)),
                )
                connection.execute(
                    "UPDATE activity_events SET scope_project_id=? WHERE request_id=?",
                    (str(result.project_id), str(result.request_id)),
                )
            kind = "identity.established" if associating else "request." + payload.state
            self._append_event(
                connection, result, kind, code=payload.error_code or payload.outcome_code
            )
        self._check(root, fd)
        if writing:
            try:
                connection.execute("COMMIT")
                self._check(root, fd)
            except Exception:
                if not connection.in_transaction:
                    raise StorageFault("storage_outcome_unconfirmed") from None
                raise
        return result

    def _release(self, connection: sqlite3.Connection | None, fd: int) -> None:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            self._close_fault = StorageFault("storage_outcome_unconfirmed")
            self._failed(self._close_fault)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    self._close_fault = StorageFault("storage_outcome_unconfirmed")
                    self._failed(self._close_fault)

    def _failed(self, fault: StorageFault) -> None:
        with self._mutex:
            self._accepting = False
            self._state = StorageReadiness(state="failed", error=fault.code)
            self._retention_position = None

    def _run(self) -> None:
        connection = None
        fd = -1
        try:
            settings, source = load_or_create_settings(self.owner)
            self._retention_days = settings.history.retention_days
            with self.owner.store.directory(self.owner.store.layout.data_dir, create=False) as root:
                connection, fd = self._open(root)
                self._publish_retention(self._activity_meta(connection))
                # Ready-time baseline; invalid time cannot trigger purge.
                self._sample_retention_wall()
                with self._mutex:
                    if self._closing.is_set():
                        self._state = StorageReadiness(state="closing")
                    else:
                        self._accepting = True
                        ready_at = self._retention_now()
                        if (
                            self._retention_progress.enabled
                            and not self._retention_progress.paused
                            and ready_at is not None
                        ):
                            self._retention_progress = self._retention_progress.model_copy(
                                update={
                                    "next_attempt_at": ready_at + RETIREMENT_INTERVAL_SECONDS,
                                }
                            )
                        self._state = StorageReadiness(
                            state="ready",
                            schema_version=DATABASE_SCHEMA,
                            retention_days=settings.history.retention_days,
                            settings_source=source,
                        )
                self._initialized.set()
                try:
                    while not self._closing.is_set() or not self._queue.empty():
                        try:
                            work = self._queue.get(timeout=0.05)
                        except queue.Empty:
                            self._maintain_due(connection, root, fd)
                            continue
                        try:
                            if self.snapshot().state == "failed":
                                if not work.result.done():
                                    work.result.set_exception(StorageFault("storage_closing"))
                                continue
                            if not work.result.set_running_or_notify_cancel():
                                continue
                            if time.monotonic() >= work.deadline:
                                raise StorageFault("storage_busy")
                            self._activity_dirty = False
                            value = self._execute(connection, work, root, fd)
                            if self._activity_dirty and self._commit_hint is not None:
                                try:
                                    self._commit_hint()
                                except Exception:
                                    # COMMIT already happened: delivery failure cannot undo it.
                                    self._notification_failed.set()
                            work.result.set_result(value)
                        except Exception as error:  # thread boundary never leaks raw driver facts
                            try:
                                if connection.in_transaction:
                                    connection.execute("ROLLBACK")
                            except sqlite3.Error:
                                fault = StorageFault("storage_outcome_unconfirmed")
                                self._failed(fault)
                                self._closing.set()
                            else:
                                fault = _fault(error)
                            if fault.code not in (
                                "storage_busy",
                                "storage_closing",
                                "registry_conflict",
                                "receipt_conflict",
                                "request_retired",
                                "receipt_scope_mismatch",
                                "activity_invalid_cursor",
                                "activity_budget_exceeded",
                                "no_history",
                            ):
                                self._failed(fault)
                                self._closing.set()
                            if not work.result.done():
                                work.result.set_exception(fault)
                        finally:
                            self._queue.task_done()
                            self._maintain_due(connection, root, fd)
                finally:
                    closing_connection, closing_fd = connection, fd
                    connection, fd = None, -1
                    self._release(closing_connection, closing_fd)
        except Exception as error:  # one owned thread's safe terminal boundary
            self._failed(self._close_fault or _fault(error))
        finally:
            self._release(connection, fd)
            with self._mutex:
                self._accepting = False
            self._initialized.set()
            while True:
                try:
                    abandoned = self._queue.get_nowait()
                except queue.Empty:
                    break
                if not abandoned.result.done():
                    abandoned.result.set_exception(StorageFault("storage_closing"))
                self._queue.task_done()
