"""Bounded retirement scheduling and checked commit-before-cache publication."""

import math
import sqlite3
from typing import TYPE_CHECKING

from jeff.application.history import ACTIVITY_EVENT_LIMIT, INT64_MAX, ActivityTrace
from jeff.application.storage import (
    RETIREMENT_BATCH_LIMIT,
    RETIREMENT_INTERVAL_SECONDS,
    RetirementBatch,
    StorageFault,
)

from .binding import _method
from .catalog import _COLUMNS, _EVENT_COLUMNS
from .security import _fault

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _retire(
    self: "SQLiteStorage", connection: sqlite3.Connection, root: int, fd: int
) -> RetirementBatch:
    """Atomically tombstone eligible completed UUIDs before deleting their details.

    Unknown/unfinished ownership remains. Publish new cursor-invalidating facts
    only after checked transaction completion; no-op priming is rolled back.
    """
    with self._mutex:
        if self._retention_progress.paused or self._closing.is_set():
            raise StorageFault("storage_closing")
    assert self._retention_days is not None
    now = self._sample_retention_wall()
    self._retention_clock_deferred = now is None
    if now is None:
        raise StorageFault("storage_busy")  # Defer; never clamp to a stale larger cutoff.
    cutoff = max(0, now - self._retention_days * 86400000)
    self._begin_write(connection, root, fd)
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
    payload_retired = self._retire_execution_payloads(connection, cutoff)
    if selected:
        connection.execute(
            "UPDATE activity_meta SET generation=generation+1,replay_floor=? WHERE singleton=1",
            (floor,),
        )
    result = RetirementBatch(
        **self._activity_meta(connection).model_dump(),
        retired_count=len(selected),
        has_more=len(rows) > RETIREMENT_BATCH_LIMIT
        or connection.execute(
            "SELECT 1 FROM execution_results WHERE stdout IS NOT NULL AND recorded_at_ms<? LIMIT 1",
            (cutoff,),
        ).fetchone()
        is not None,
    )
    self._check(root, fd)
    try:
        if selected or payload_retired:
            self._commit_write(connection, fd)
        else:
            self._rollback_write(connection, fd)
        self._check(root, fd)
    except Exception:
        if not connection.in_transaction:
            raise StorageFault("storage_outcome_unconfirmed") from None
        raise
    self._publish_retention(result)  # Checked transaction completion precedes the cache.
    self._activity_dirty = bool(selected)
    return result


@_method
def _sample_retention_wall(self: "SQLiteStorage") -> int | None:
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


@_method
def _retention_now(self: "SQLiteStorage") -> float | None:
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


@_method
def _retention_end(
    self: "SQLiteStorage",
    *,
    batch: RetirementBatch | None = None,
    fault: StorageFault | None = None,
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


@_method
def _maintain_due(
    self: "SQLiteStorage", connection: sqlite3.Connection, root: int, fd: int
) -> None:
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
                self._rollback_write(connection, fd)
        except sqlite3.Error:
            fault = StorageFault("storage_outcome_unconfirmed")
        else:
            fault = _fault(error)
        if fault.code not in ("storage_busy", "storage_closing"):
            self._failed(fault)
            self._closing.set()
        self._retention_end(fault=fault)
