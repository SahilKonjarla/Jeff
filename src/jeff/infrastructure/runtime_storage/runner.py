"""One connection-owning thread, exact release lifetimes and terminal queue drainage."""

import os
import queue
import sqlite3
import time
from typing import TYPE_CHECKING

from jeff.application.execution import DockerBackendFault, ReviewFault
from jeff.application.storage import (
    DATABASE_SCHEMA,
    RETIREMENT_INTERVAL_SECONDS,
    ConfiguredRuntimeSettings,
    StorageFault,
    StorageReadiness,
)
from jeff.infrastructure.runtime_settings import load_or_create_settings

from .binding import _method
from .security import _fault

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _release(self: "SQLiteStorage", connection: sqlite3.Connection | None, fd: int) -> None:
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


@_method
def _failed(self: "SQLiteStorage", fault: StorageFault) -> None:
    with self._mutex:
        self._accepting = False
        self._state = StorageReadiness(state="failed", error=fault.code)
        self._retention_position = None
        self._execution_backend = None


@_method
def _run(self: "SQLiteStorage") -> None:
    connection = None
    fd = -1
    try:
        settings, source = load_or_create_settings(self.owner)
        self._retention_days = settings.history.retention_days
        self._execution_profiles = settings.execution.profiles
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
                    self._execution_backend = (
                        settings.backend
                        if isinstance(settings, ConfiguredRuntimeSettings)
                        else None
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
                                self._rollback_write(connection, fd)
                        except Exception:  # Resolve active work even if proof retirement fails.
                            fault = StorageFault("storage_outcome_unconfirmed")
                            self._failed(fault)
                            self._closing.set()
                        else:
                            if isinstance(error, (ReviewFault, DockerBackendFault)):
                                if not work.result.done():
                                    work.result.set_exception(error)
                                continue
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
            self._execution_backend = None
        self._initialized.set()
        while True:
            try:
                abandoned = self._queue.get_nowait()
            except queue.Empty:
                break
            if not abandoned.result.done():
                abandoned.result.set_exception(StorageFault("storage_closing"))
            self._queue.task_done()
