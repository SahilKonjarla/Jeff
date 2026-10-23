"""Private SQLite registry with one connection-owning thread and bounded declared work.

Python SQLite opens a pathname, not our checked descriptor. Private ancestry plus
before/after identity checks narrow races; this is not a hostile same-user/ACL proof.
"""

import os
import queue
import sqlite3
import stat
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault
from jeff.application.storage import (
    DATABASE_APPLICATION_ID,
    DATABASE_SCHEMA,
    STORAGE_PENDING_LIMIT,
    Receipt,
    ReceiptClaim,
    ReceiptClaimResult,
    ReceiptScope,
    ReceiptTransition,
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
    if version not in (0, 1, DATABASE_SCHEMA):
        raise StorageFault("storage_schema_unsupported")
    if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
        raise StorageFault("storage_recovery_required")
    expected = {"schema_migrations": ("table", _LEDGER)}
    if version >= 1:
        expected["projects"] = ("table", _PROJECTS)
    if version >= 2:
        expected["request_receipts"] = ("table", _RECEIPTS)
        expected["receipts_unfinished"] = ("index", _RECEIPT_INDEX)
    found = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 5"
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
        "SELECT version,name FROM schema_migrations ORDER BY version LIMIT 3"
    ).fetchall()
    if ledger != [
        (number, name)
        for number, name in ((1, "registry_v1"), (2, "request_receipts_v2"))
        if number <= version
    ]:
        raise StorageFault("storage_schema_unsupported")
    return version


@dataclass
class _Work:
    operation: Literal["index", "lookup", "claim", "finish", "inspect", "root_claimed"]
    payload: Project | ReceiptClaim | ReceiptTransition | ReceiptScope | Path
    deadline: float
    result: Future[object]


class SQLiteStorage:
    """Start/close belong to explicit lifetime ownership; registry calls block callers,
    never the control loop. No SQL/connection/cursor is exposed through this interface.
    """

    def __init__(self, owner: Ownership, *, instance_id: UUID | None = None) -> None:
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

    def _submit(
        self,
        operation: Literal["index", "lookup", "claim", "finish", "inspect", "root_claimed"],
        payload: Project | ReceiptClaim | ReceiptTransition | ReceiptScope | Path,
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
            _schema(connection)
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
    def _reconcile(connection: sqlite3.Connection, instance: UUID) -> None:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.start_seconds
        while True:
            if time.monotonic() >= deadline:
                raise StorageFault("storage_busy")
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT request_id FROM request_receipts WHERE state IN ('accepted','running') "
                "AND owner_instance_id<>? LIMIT 100",
                (str(instance),),
            ).fetchall()
            if not rows:
                connection.execute("COMMIT")
                return
            for (request_id,) in rows:
                connection.execute(
                    "UPDATE request_receipts SET state='unknown',finished_at_ms=?,"
                    "outcome_code='interrupted' WHERE request_id=?",
                    (time.time_ns() // 1000000, request_id),
                )
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

    def _run(self) -> None:
        connection = None
        fd = -1
        try:
            settings, source = load_or_create_settings(self.owner)
            with self.owner.store.directory(self.owner.store.layout.data_dir, create=False) as root:
                connection, fd = self._open(root)
                with self._mutex:
                    if self._closing.is_set():
                        self._state = StorageReadiness(state="closing")
                    else:
                        self._accepting = True
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
                            work.result.set_result(self._execute(connection, work, root, fd))
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
                                "registry_conflict",
                                "receipt_conflict",
                                "receipt_scope_mismatch",
                            ):
                                self._failed(fault)
                                self._closing.set()
                            if not work.result.done():
                                work.result.set_exception(fault)
                        finally:
                            self._queue.task_done()
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
