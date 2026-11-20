"""Scoped receipts, interrupted-owner reconciliation and atomic receipt transitions."""

import sqlite3
import time
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS
from jeff.application.storage import (
    Receipt,
    ReceiptClaim,
    ReceiptClaimResult,
    ReceiptScope,
    ReceiptTransition,
    StorageFault,
)

from .. import runtime_storage as _state
from .binding import _method
from .catalog import _COLUMNS
from .work import _Work

if TYPE_CHECKING:
    from .store import SQLiteStorage


def _scoped_receipt(connection: sqlite3.Connection, scope: ReceiptScope) -> Receipt | None:
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM request_receipts WHERE request_id=?", (str(scope.request_id),)
    ).fetchone()
    receipt = _state.SQLiteStorage._receipt(row) if row is not None else None
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


@_method
def _reconcile(
    self: "SQLiteStorage", connection: sqlite3.Connection, instance: UUID, root: int, fd: int
) -> None:
    """Mark prior-owner unfinished receipts interrupted/unknown, never re-execute them."""
    deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.start_seconds
    while True:
        if time.monotonic() >= deadline:
            raise StorageFault("storage_busy")
        self._begin_write(connection, root, fd)
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM request_receipts WHERE state IN ('accepted','running') "
            "AND owner_instance_id<>? LIMIT 100",
            (str(instance),),
        ).fetchall()
        if not rows:
            self._rollback_write(connection, fd)  # Undo the primer when there is no work.
            return
        for row in rows:
            receipt = self._receipt(row)
            connection.execute(
                "UPDATE request_receipts SET state='unknown',finished_at_ms=?,"
                "outcome_code='interrupted' WHERE request_id=?",
                (time.time_ns() // 1000000, str(receipt.request_id)),
            )
            self._append_event(connection, receipt, "request.interrupted", code="interrupted")
        self._commit_write(connection, fd)


@_method
def _receipt_work(
    self: "SQLiteStorage", connection: sqlite3.Connection, work: _Work, root: int, fd: int
) -> object:
    payload = work.payload
    assert isinstance(payload, (ReceiptClaim, ReceiptTransition, ReceiptScope))
    writing = work.operation != "inspect"
    if writing:
        self._begin_write(connection, root, fd)
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
            raise StorageFault("storage_unrecognized" if receipt is not None else "request_retired")
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
            if isinstance(result, ReceiptClaimResult) and not result.created:
                self._rollback_write(connection, fd)
            else:
                self._commit_write(connection, fd)
            self._check(root, fd)
        except Exception:
            if not connection.in_transaction:
                raise StorageFault("storage_outcome_unconfirmed") from None
            raise
    return result
