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
from typing import Literal
from uuid import UUID

from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS, ControlFault
from jeff.application.storage import (
    DATABASE_APPLICATION_ID,
    DATABASE_SCHEMA,
    STORAGE_PENDING_LIMIT,
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
    if version not in (0, DATABASE_SCHEMA):
        raise StorageFault("storage_schema_unsupported")
    if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
        raise StorageFault("storage_recovery_required")
    expected = {"schema_migrations": _LEDGER}
    if version == DATABASE_SCHEMA:
        expected["projects"] = _PROJECTS
    found = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 3"
    ).fetchall()

    def normalize(text: object) -> str:
        return " ".join(str(text).upper().split())

    if len(found) != len(expected) or any(
        kind != "table" or name not in expected or normalize(sql) != normalize(expected[name])
        for kind, name, sql in found
    ):
        raise StorageFault("storage_schema_unsupported")
    ledger = connection.execute(
        "SELECT version,name FROM schema_migrations ORDER BY version LIMIT 2"
    ).fetchall()
    if ledger != ([] if version == 0 else [(1, "registry_v1")]):
        raise StorageFault("storage_schema_unsupported")
    return version


@dataclass
class _Work:
    operation: Literal["index", "lookup"]
    project: Project
    deadline: float
    result: Future[bool]


class SQLiteStorage:
    """Start/close belong to explicit lifetime ownership; registry calls block callers,
    never the control loop. No SQL/connection/cursor is exposed through this interface.
    """

    def __init__(self, owner: Ownership) -> None:
        self.owner = owner
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

    def index(self, project: Project) -> tuple[Project, bool]:
        return project, self._submit("index", project)

    def lookup(self, project: Project) -> Project | None:
        return project if self._submit("lookup", project) else None

    def _submit(self, operation: Literal["index", "lookup"], project: Project) -> bool:
        if (
            not isinstance(project.project_id, UUID)
            or not project.root.is_absolute()
            or "\0" in str(project.root)
        ):
            raise StorageFault("registry_conflict")
        result: Future[bool] = Future()
        with self._mutex:
            if not self._accepting:
                raise StorageFault("storage_closing")
            try:
                self._queue.put_nowait(
                    _Work(
                        operation,
                        project,
                        time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds,
                        result,
                    )
                )
            except queue.Full:
                raise StorageFault("storage_busy") from None
        try:
            return result.result(timeout=DEFAULT_RUNTIME_LIMITS.request_seconds)
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
            _schema(connection)
            self._check(root, fd)
            return connection, fd
        except BaseException:
            try:
                if connection is not None and connection.in_transaction:
                    connection.execute("ROLLBACK")
            finally:
                self._release(connection, fd)
            raise

    def _execute(self, connection: sqlite3.Connection, work: _Work, root: int, fd: int) -> bool:
        self._check(root, fd)
        project_id, path = str(work.project.project_id), str(work.project.root)
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
                            if fault.code not in ("storage_busy", "registry_conflict"):
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
