"""Receipt/event decoding, bounded append and committed activity metadata."""

import sqlite3
import time
from typing import TYPE_CHECKING

from jeff.application.history import (
    ACTIVITY_EVENT_LIMIT,
    ActivityEvent,
    ActivityMeta,
    ActivityRecord,
    activity_code,
)
from jeff.application.storage import Receipt, StorageFault

from .binding import _method
from .catalog import _COLUMNS

if TYPE_CHECKING:
    from .store import SQLiteStorage


def _receipt(row: tuple[object, ...]) -> Receipt:
    return Receipt.model_validate(dict(zip(_COLUMNS.split(","), row, strict=True)))


def _activity_meta(connection: sqlite3.Connection, *, legacy: bool = False) -> ActivityMeta:
    fields = "epoch,coverage_started_at_ms,high_water,generation"
    row = connection.execute(
        f"SELECT {fields}{'' if legacy else ',replay_floor'} FROM activity_meta WHERE singleton=1"
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


@_method
def _append_event(
    self: "SQLiteStorage",
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
    connection.execute("UPDATE activity_meta SET high_water=? WHERE singleton=1", (event.sequence,))
    self._activity_dirty = True
    return event
