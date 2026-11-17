"""Scope-bound history/stream/replay reads and bounded activity observations."""

import sqlite3
from typing import TYPE_CHECKING

from jeff.application.history import (
    ActivityEvent,
    ActivityPage,
    ActivityRecord,
    ActivityTrace,
    HistoryCursor,
    HistoryItem,
    HistoryPage,
    HistoryRead,
    HistoryScope,
    ReplayScope,
    StreamCursor,
    StreamPage,
    StreamRead,
)
from jeff.application.storage import ReceiptScope, StorageFault

from .binding import _method
from .catalog import _COLUMNS, _EVENT_COLUMNS
from .work import _Work

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _stream_page(
    self: "SQLiteStorage", connection: sqlite3.Connection, scope: StreamRead, root: int, fd: int
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


@_method
def _history(
    self: "SQLiteStorage", connection: sqlite3.Connection, scope: HistoryRead, root: int, fd: int
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


@_method
def _activity_work(
    self: "SQLiteStorage", connection: sqlite3.Connection, work: _Work, root: int, fd: int
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
                payload.expected_epoch is not None and payload.expected_epoch != meta.database_epoch
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
    self._begin_write(connection, root, fd)
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
        self._commit_write(connection, fd)
        self._check(root, fd)
    except Exception:
        if not connection.in_transaction:
            raise StorageFault("storage_outcome_unconfirmed") from None
        raise
    return event
