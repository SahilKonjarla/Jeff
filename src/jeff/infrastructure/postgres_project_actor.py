"""Isolated project-only daemon storage on one PostgreSQL writer thread."""

import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextlib import AbstractContextManager
from pathlib import Path
from threading import BoundedSemaphore
from typing import Literal, TypeVar
from uuid import UUID

from jeff.application.execution import ExecutionInspection, ExecutionInspectionScope, ReviewFault
from jeff.application.execution.watch import WatchInbox, WatchScope
from jeff.application.history import (
    ActivityEvent,
    ActivityPage,
    ActivityRecord,
    ActivityTrace,
    HistoryPage,
    HistoryRead,
    HistoryScope,
    ReplayScope,
    RetentionPosition,
    StreamPage,
    StreamRead,
)
from jeff.application.storage import (
    Receipt,
    ReceiptClaim,
    ReceiptClaimResult,
    ReceiptScope,
    ReceiptTransition,
    RetirementBatch,
    StorageFault,
    StorageReadiness,
)
from jeff.domain.projects import Project

from . import postgres_activity, postgres_execution, postgres_inspection, postgres_payloads
from .postgres_storage import PostgresStorage

T = TypeVar("T")


class PostgresProjectActor:
    """Proof composition only. Normal Jeff startup does not select this actor."""

    def __init__(
        self,
        adapter: PostgresStorage,
        *,
        on_commit: Callable[[], None],
        retention_days: int = 30,
        settings_source: Literal["default_file", "existing_file"] = "default_file",
    ) -> None:
        self._adapter = adapter
        self._on_commit = on_commit
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jeff-pg-proof")
        self._slots = BoundedSemaphore(7)
        self._control_slot = BoundedSemaphore(1)
        self._retention_days = retention_days
        self._settings_source = settings_source
        self._retention = postgres_activity.RetentionSchedule(days=retention_days)
        self._writer: AbstractContextManager[None] | None = None
        self._state = StorageReadiness(state="initializing")
        self._position: RetentionPosition | None = None

    def start(self) -> None:
        def initialize() -> None:
            writer = self._adapter.writer()
            writer.__enter__()
            self._writer = writer
            with self._adapter._transaction(time.monotonic() + 4, write=True) as connection:
                # The original writer lock is held now. Do not re-admit prior work.
                rows = connection.execute(
                    "SELECT DISTINCT prior FROM ("
                    "SELECT payload->>'owner_instance_id' AS prior FROM jeff_proof.receipts "
                    "WHERE payload->>'state' IN ('accepted','running') UNION "
                    "SELECT payload->>'owner_instance_id' AS prior FROM jeff_proof.authorities "
                    "WHERE payload->>'state' IN ('pending','approved') "
                    "OR payload->>'claim_state'='claimed') owners WHERE prior<>%s LIMIT 9",
                    (str(self._adapter.instance_id),),
                ).fetchall()
                if len(rows) > 8:
                    raise StorageFault("storage_recovery_required")
                for (prior,) in rows:
                    if not isinstance(prior, str):
                        raise StorageFault("storage_unrecognized")
                    old = UUID(prior)
                    if (
                        postgres_activity.reconcile(connection, old, time.time_ns() // 1_000_000)
                        >= 100
                        or postgres_execution.fence(connection, old) >= 100
                    ):
                        raise StorageFault("storage_recovery_required")
                facts = postgres_activity.meta(connection)
            self._position = RetentionPosition(
                database_epoch=facts.database_epoch,
                retention_generation=facts.retention_generation,
                replay_floor=facts.replay_floor,
            )
            self._state = StorageReadiness(
                state="ready",
                backend="postgresql",
                schema_version=1,
                retention_days=self._retention_days,
                settings_source=self._settings_source,
            )

        self._pool.submit(initialize).result(timeout=7)

    def close(self) -> None:
        self._state = StorageReadiness(state="closing")

        def close_writer() -> None:
            if self._writer is not None:
                self._writer.__exit__(None, None, None)

        try:
            self._pool.submit(close_writer).result(timeout=7)
        finally:
            self._pool.shutdown()

    def snapshot(self) -> StorageReadiness:
        return self._state

    def retention_position(self) -> RetentionPosition | None:
        return self._position

    def maintain_retention(self) -> None:
        def tick(end: float) -> None:
            def retire(cutoff: int) -> RetirementBatch:
                with self._adapter._transaction(end, write=True) as connection:
                    batch = postgres_activity.retire(connection, cutoff)
                    postgres_payloads.retire(connection, cutoff)
                self._position = RetentionPosition(
                    database_epoch=batch.database_epoch,
                    retention_generation=batch.retention_generation,
                    replay_floor=batch.replay_floor,
                )
                return batch

            self._retention.tick(
                time.time_ns() // 1_000_000, time.monotonic_ns() // 1_000_000, retire
            )

        self._call(tick, None, write=True)

    def _submit(
        self,
        operation: Callable[[float], T],
        end: float,
        *,
        write: bool = False,
        control: bool = False,
    ) -> Future[T]:
        slots = self._control_slot if control else self._slots
        if self._state.state != "ready":
            raise StorageFault("storage_closing")
        if end <= time.monotonic() or not slots.acquire(blocking=False):
            raise StorageFault("storage_busy")

        def run() -> T:
            try:
                if self._state.state != "ready":
                    raise StorageFault("storage_closing")
                result = operation(end)
            except StorageFault as error:
                if error.code in {
                    "storage_failed",
                    "storage_outcome_unconfirmed",
                    "storage_closing",
                }:
                    self._state = StorageReadiness(state="failed", error=error.code)
                raise
            if write:
                self._on_commit()
            return result

        try:
            future = self._pool.submit(run)
        except RuntimeError:
            slots.release()
            raise StorageFault("storage_closing") from None
        future.add_done_callback(lambda _: slots.release())
        return future

    def _call(
        self, operation: Callable[[float], T], deadline: float | None, *, write: bool = False
    ) -> T:
        end = time.monotonic() + 4 if deadline is None else deadline
        future = self._submit(operation, end, write=write)
        try:
            return future.result(timeout=max(0, end - time.monotonic()))
        except TimeoutError:
            if future.cancel() or not write:
                raise StorageFault("storage_busy") from None
            raise StorageFault("storage_outcome_unconfirmed") from None

    def index(self, project: Project, *, deadline: float | None = None) -> tuple[Project, bool]:
        return self._call(
            lambda end: self._adapter.index(project, deadline=end), deadline, write=True
        )

    def lookup(self, project: Project, *, deadline: float | None = None) -> Project | None:
        return self._call(lambda end: self._adapter.lookup(project, deadline=end), deadline)

    def root_claimed(self, root: Path, *, deadline: float | None = None) -> bool:
        return self._call(lambda end: self._adapter.root_claimed(root, deadline=end), deadline)

    def claim(self, claim: ReceiptClaim, *, deadline: float) -> ReceiptClaimResult:
        return self._call(
            lambda end: self._adapter.claim(claim, deadline=end), deadline, write=True
        )

    def finish(self, transition: ReceiptTransition, *, deadline: float) -> Receipt:
        return self._call(
            lambda end: self._adapter.finish(transition, deadline=end), deadline, write=True
        )

    def inspect(self, scope: ReceiptScope, *, deadline: float) -> Receipt | None:
        return self._call(lambda end: self._adapter.inspect(scope, deadline=end), deadline)

    def observe(self, record: ActivityRecord, *, deadline: float) -> ActivityEvent:
        return self._call(
            lambda end: self._adapter.observe(record, deadline=end), deadline, write=True
        )

    def trace(
        self, scope: ReceiptScope, *, deadline: float
    ) -> tuple[Receipt | None, ActivityTrace | None]:
        return self._call(lambda end: self._adapter.trace(scope, deadline=end), deadline)

    def debug(self, scope: HistoryScope, *, deadline: float) -> tuple[Receipt, ActivityTrace]:
        return self._call(lambda end: self._adapter.debug(scope, deadline=end), deadline)

    def replay(self, scope: ReplayScope, *, deadline: float) -> ActivityPage:
        return self._call(lambda end: self._adapter.replay(scope, deadline=end), deadline)

    def history(self, scope: HistoryRead, *, deadline: float) -> HistoryPage:
        return self._call(lambda end: self._adapter.history(scope, deadline=end), deadline)

    def stream_page(self, scope: StreamRead, *, deadline: float) -> StreamPage:
        return self._call(lambda end: self._adapter.stream_page(scope, deadline=end), deadline)

    def inspect_execution(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionInspection:
        if scope.owner_instance_id != self._adapter.instance_id:
            raise ReviewFault("execution_not_found")

        def read(end: float) -> ExecutionInspection:
            with self._adapter._transaction(end, write=False) as connection:
                return postgres_inspection.inspect(
                    connection, scope, retention_days=self._retention_days
                )

        return self._call(read, deadline)

    def watch(self, scope: WatchScope, *, deadline: float) -> WatchInbox:
        return self._call(
            lambda end: self._adapter.watches.watch(scope, deadline=end),
            deadline,
            write=scope.operation != "watch.pending",
        )
