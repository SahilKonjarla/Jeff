"""Transaction-local receipt history, stream positions and retirement for the PG proof."""

from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.history import (
    ACTIVITY_EVENT_LIMIT,
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
    StreamCursor,
    StreamPage,
    StreamRead,
)
from jeff.application.storage import (
    RETIREMENT_BATCH_LIMIT,
    Receipt,
    ReceiptScope,
    RetirementBatch,
    StorageFault,
)

if TYPE_CHECKING:
    from .postgres_storage import Connection

ACTIVITY_DDL = (
    "CREATE TABLE jeff_proof.activity (singleton bool PRIMARY KEY CHECK (singleton), "
    "database_epoch uuid NOT NULL, coverage_started_at_ms bigint NOT NULL, "
    "high_water bigint NOT NULL DEFAULT 0, retention_generation bigint NOT NULL DEFAULT 0, "
    "replay_floor bigint NOT NULL DEFAULT 0)",
    "CREATE TABLE jeff_proof.events (sequence bigint PRIMARY KEY, request_id uuid NOT NULL "
    "REFERENCES jeff_proof.receipts(request_id) ON DELETE CASCADE, payload jsonb NOT NULL)",
    "CREATE INDEX proof_events_request ON jeff_proof.events(request_id,sequence)",
    "CREATE TABLE jeff_proof.tombstones (request_id uuid PRIMARY KEY)",
)


def receipt_for(
    connection: "Connection", request_id: UUID, *, locked: bool = False
) -> Receipt | None:
    row = connection.execute(
        "SELECT payload FROM jeff_proof.receipts WHERE request_id=%s"
        + (" FOR UPDATE" if locked else ""),
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = Receipt.model_validate(row[0])
    if receipt.request_id != request_id:
        raise StorageFault("storage_unrecognized")
    return receipt


def scoped_receipt(connection: "Connection", scope: ReceiptScope) -> Receipt | None:
    receipt = receipt_for(connection, scope.request_id)
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


def meta(connection: "Connection", *, locked: bool = False) -> ActivityMeta:
    row = connection.execute(
        "SELECT database_epoch,coverage_started_at_ms,high_water,retention_generation,"
        "replay_floor FROM jeff_proof.activity WHERE singleton" + (" FOR UPDATE" if locked else "")
    ).fetchone()
    if row is None:
        raise StorageFault("storage_unrecognized")
    return ActivityMeta.model_validate(dict(zip(ActivityMeta.model_fields, row, strict=True)))


def append_event(
    connection: "Connection",
    receipt: Receipt,
    *,
    kind: str,
    at: int,
    code: str | None = None,
    observation: ActivityRecord | None = None,
) -> ActivityEvent:
    count = connection.execute(
        "SELECT count(*) FROM jeff_proof.events WHERE request_id=%s", (receipt.request_id,)
    ).fetchone()
    terminal = kind in {
        "request.succeeded",
        "request.failed",
        "request.unknown",
        "request.interrupted",
    }
    budget = ACTIVITY_EVENT_LIMIT if terminal else ACTIVITY_EVENT_LIMIT - 2
    if count is None or not isinstance(count[0], int) or count[0] >= budget:
        raise StorageFault("activity_budget_exceeded")
    row = connection.execute(
        "UPDATE jeff_proof.activity SET high_water=high_water+1 "
        "WHERE singleton RETURNING high_water"
    ).fetchone()
    if row is None:
        raise StorageFault("storage_unrecognized")
    event = ActivityEvent.model_validate(
        dict(
            sequence=row[0],
            request_id=receipt.request_id,
            owner_instance_id=receipt.owner_instance_id,
            canonical_root=receipt.canonical_root,
            project_id=receipt.project_id,
            kind=kind,
            recorded_at_ms=at,
            code=code,
            git=observation.git if observation is not None else None,
            registration_created=(observation.registration_created if observation else None),
        ),
    )
    connection.execute(
        "INSERT INTO jeff_proof.events VALUES (%s,%s,%s::jsonb)",
        (event.sequence, event.request_id, event.model_dump_json()),
    )
    return event


def trace(connection: "Connection", receipt: Receipt) -> ActivityTrace:
    facts = meta(connection)
    rows = connection.execute(
        "SELECT payload FROM jeff_proof.events WHERE request_id=%s ORDER BY sequence LIMIT %s",
        (receipt.request_id, ACTIVITY_EVENT_LIMIT + 1),
    ).fetchall()
    return ActivityTrace(
        **facts.model_dump(), events=tuple(ActivityEvent.model_validate(row[0]) for row in rows)
    )


def latest_request(connection: "Connection", scope: HistoryScope) -> UUID:
    row = connection.execute(
        "SELECT r.request_id FROM jeff_proof.receipts r "
        "JOIN jeff_proof.events e USING (request_id) "
        "WHERE r.payload->>'project_id'=%s AND r.payload->>'canonical_root'=%s "
        "AND e.payload->>'kind'='request.accepted' ORDER BY e.sequence DESC LIMIT 1",
        (str(scope.project_id), str(scope.canonical_root)),
    ).fetchone()
    if row is None:
        raise StorageFault("no_history")
    if not isinstance(row[0], UUID):
        raise StorageFault("storage_unrecognized")
    return row[0]


def replay(connection: "Connection", scope: ReplayScope) -> ActivityPage:
    facts = meta(connection)
    if not facts.replay_floor <= scope.after <= facts.high_water or (
        scope.expected_epoch is not None and scope.expected_epoch != facts.database_epoch
    ):
        raise StorageFault("activity_invalid_cursor")
    rows = connection.execute(
        "SELECT e.payload FROM jeff_proof.events e JOIN jeff_proof.receipts r USING (request_id) "
        "WHERE r.payload->>'project_id'=%s AND r.payload->>'canonical_root'=%s "
        "AND e.sequence>%s AND e.sequence<=%s ORDER BY e.sequence LIMIT %s",
        (
            str(scope.project_id),
            str(scope.canonical_root),
            scope.after,
            facts.high_water,
            scope.limit + 1,
        ),
    ).fetchall()
    events = tuple(ActivityEvent.model_validate(row[0]) for row in rows[: scope.limit])
    return ActivityPage(
        **facts.model_dump(),
        events=events,
        has_more=len(rows) > scope.limit,
        next_after=events[-1].sequence if events else scope.after,
    )


def stream(connection: "Connection", scope: StreamRead) -> StreamPage:
    facts = meta(connection)
    after = facts.high_water
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
            or cursor.database_epoch != facts.database_epoch
            or cursor.retention_generation != facts.retention_generation
            or not facts.replay_floor <= cursor.after <= facts.high_water
        ):
            raise StorageFault("activity_invalid_cursor")
        after = cursor.after
        rows = connection.execute(
            "SELECT payload FROM jeff_proof.events WHERE sequence>%s AND sequence<=%s "
            "AND payload->>'project_id'=%s AND payload->>'canonical_root'=%s "
            "ORDER BY sequence LIMIT %s",
            (
                after,
                facts.high_water,
                str(scope.project_id),
                str(scope.canonical_root),
                scope.limit + 1,
            ),
        ).fetchall()
        events = tuple(ActivityEvent.model_validate(row[0]) for row in rows[: scope.limit])
        more = len(rows) > scope.limit
    return StreamPage(
        **facts.model_dump(),
        project_id=scope.project_id,
        canonical_root=scope.canonical_root,
        start_after=after,
        scanned_through=events[-1].sequence if more else facts.high_water,
        events=events,
        has_more=more,
    )


def history(connection: "Connection", scope: HistoryRead) -> HistoryPage:
    facts = meta(connection)
    ceiling = facts.high_water
    before = ceiling
    seek = "<="
    project, root = str(scope.project_id), str(scope.canonical_root)
    if scope.cursor is not None:
        try:
            cursor = HistoryCursor.decode(scope.cursor)
        except ValueError:
            raise StorageFault("activity_invalid_cursor") from None
        if (
            cursor.project_id != scope.project_id
            or cursor.canonical_root != scope.canonical_root
            or cursor.database_epoch != facts.database_epoch
            or cursor.retention_generation != facts.retention_generation
            or cursor.snapshot_high_water > facts.high_water
            or connection.execute(
                "SELECT 1 FROM jeff_proof.events e JOIN jeff_proof.receipts r USING (request_id) "
                "WHERE e.sequence=%s AND e.payload->>'kind'='request.accepted' "
                "AND r.payload->>'project_id'=%s AND r.payload->>'canonical_root'=%s",
                (cursor.before, project, root),
            ).fetchone()
            is None
        ):
            raise StorageFault("activity_invalid_cursor")
        ceiling, before, seek = cursor.snapshot_high_water, cursor.before, "<"
    rows = connection.execute(
        "SELECT r.payload,e.sequence FROM jeff_proof.receipts r "
        "JOIN jeff_proof.events e USING (request_id) WHERE e.payload->>'kind'='request.accepted' "
        "AND r.payload->>'project_id'=%s AND r.payload->>'canonical_root'=%s "
        f"AND e.sequence {seek} %s AND NOT EXISTS (SELECT 1 FROM jeff_proof.events later "
        "WHERE later.request_id=r.request_id AND later.payload->>'kind'='identity.established' "
        "AND later.sequence>%s) ORDER BY e.sequence DESC LIMIT %s",
        (project, root, before, ceiling, scope.limit + 1),
    ).fetchall()
    items = tuple(
        HistoryItem.model_validate(
            Receipt.model_validate(payload).model_dump(exclude={"owner_instance_id"})
            | {"accepted_sequence": sequence}
        )
        for payload, sequence in rows[: scope.limit]
    )
    next_cursor = None
    if len(rows) > scope.limit:
        next_cursor = HistoryCursor(
            project_id=scope.project_id,
            canonical_root=scope.canonical_root,
            database_epoch=facts.database_epoch,
            retention_generation=facts.retention_generation,
            snapshot_high_water=ceiling,
            before=items[-1].accepted_sequence,
        ).encode()
    return HistoryPage(
        **facts.model_dump(), snapshot_high_water=ceiling, items=items, next_cursor=next_cursor
    )


def retire(connection: "Connection", cutoff: int) -> RetirementBatch:
    facts = meta(connection, locked=True)
    rows = connection.execute(
        "SELECT request_id FROM jeff_proof.receipts "
        "WHERE payload->>'state' IN ('succeeded','failed') "
        "AND (payload->>'finished_at_ms')::bigint<%s "
        "ORDER BY (payload->>'finished_at_ms')::bigint, request_id LIMIT %s",
        (cutoff, RETIREMENT_BATCH_LIMIT + 1),
    ).fetchall()
    ids = [row[0] for row in rows[:RETIREMENT_BATCH_LIMIT]]
    if ids:
        connection.execute("INSERT INTO jeff_proof.tombstones SELECT unnest(%s::uuid[])", (ids,))
        connection.execute(
            "UPDATE jeff_proof.activity SET retention_generation=retention_generation+1, "
            "replay_floor=GREATEST(replay_floor,(SELECT max(sequence) FROM jeff_proof.events "
            "WHERE request_id=ANY(%s::uuid[]))) WHERE singleton",
            (ids,),
        )
        connection.execute(
            "DELETE FROM jeff_proof.receipts WHERE request_id=ANY(%s::uuid[])", (ids,)
        )
        facts = meta(connection)
    return RetirementBatch(
        **facts.model_dump(), retired_count=len(ids), has_more=len(rows) > RETIREMENT_BATCH_LIMIT
    )


def reconcile(connection: "Connection", prior_owner: UUID, at: int) -> int:
    meta(connection, locked=True)
    rows = connection.execute(
        "SELECT payload FROM jeff_proof.receipts WHERE payload->>'owner_instance_id'=%s "
        "AND payload->>'state' IN ('accepted','running') ORDER BY request_id LIMIT 100",
        (str(prior_owner),),
    ).fetchall()
    for (payload,) in rows:
        receipt = Receipt.model_validate(payload)
        terminal = Receipt.model_validate(
            receipt.model_dump()
            | {"state": "unknown", "finished_at_ms": at, "outcome_code": "interrupted"}
        )
        connection.execute(
            "UPDATE jeff_proof.receipts SET payload=%s::jsonb WHERE request_id=%s",
            (terminal.model_dump_json(), terminal.request_id),
        )
        append_event(connection, terminal, kind="request.interrupted", at=at, code="interrupted")
    return len(rows)


class RetentionSchedule:
    """Proof-only tick gate; no background thread or auto-retry of uncertain writes."""

    def __init__(self, *, days: int = 30, interval_ms: int = 60_000) -> None:
        if days < 1 or interval_ms < 1:
            raise ValueError("Invalid retention schedule.")
        self._age = days * 86_400_000
        self._interval = interval_ms
        self._last: tuple[int, int] | None = None
        self._due: int | None = None
        self._paused = False

    def tick(
        self, wall_ms: int, monotonic_ms: int, retire_before: Callable[[int], RetirementBatch]
    ) -> RetirementBatch | None:
        if self._paused or wall_ms < 0 or monotonic_ms < 0:
            return None
        if self._last is not None:
            prev_wall, prev_mono = self._last
            wall_delta, mono_delta = wall_ms - prev_wall, monotonic_ms - prev_mono
            if min(wall_delta, mono_delta) < 0 or abs(wall_delta - mono_delta) > 60_000:
                return None  # Never advance the cutoff from a suspect clock sample.
        self._last = (wall_ms, monotonic_ms)
        if self._due is None:
            self._due = monotonic_ms + self._interval
        if monotonic_ms < self._due:
            return None
        try:
            result = retire_before(max(0, wall_ms - self._age))
        except StorageFault as error:
            if error.code != "storage_busy":
                self._paused = True
            self._due = monotonic_ms + self._interval
            raise
        self._due = monotonic_ms + self._interval
        return result
