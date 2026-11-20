"""Durable stop fence and terminal recording on the same SQL owner/transaction."""

import sqlite3
from typing import TYPE_CHECKING, cast
from uuid import UUID

from jeff.application.execution import (
    DockerLaunchScope,
    DockerOutputSnapshot,
    ExecutionInspectionScope,
    ExecutionStopCapture,
    ExecutionStopObservation,
    ExecutionStopRecord,
    ExecutionStopRequest,
    ExecutionStopTerminal,
    ReviewFault,
)
from jeff.application.history import INT64_MAX
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState, fingerprint

from .binding import _method

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _request_stop(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    request: ExecutionStopRequest,
    root: int,
    fd: int,
) -> ExecutionStopRecord:
    scope = ExecutionInspectionScope.model_validate(
        request.model_dump(
            include={
                "project_id",
                "canonical_root",
                "owner_instance_id",
                "approval_id",
            }
        )
    )
    authority = self._stop_target(connection, scope)
    if request.manifest_digest != authority.manifest_digest:
        raise ReviewFault("execution_target_mismatch")
    self._begin_write(connection, root, fd)
    digest = fingerprint(
        {
            "operation": "stop_v1",
            **request.model_dump(
                mode="json",
                exclude={"request_id"},
            ),
        }
    )
    if connection.execute(
        "SELECT 1 FROM execution_operations WHERE request_id=?", (str(request.request_id),)
    ).fetchone():
        raise ReviewFault("execution_conflict")
    prior = connection.execute(
        "SELECT payload_digest,proposal_id FROM execution_authority_operations WHERE request_id=?",
        (str(request.request_id),),
    ).fetchone()
    if prior is not None and prior != (digest, str(authority.proposal_id)):
        raise ReviewFault("execution_conflict")
    record = self._read_stop(connection, authority, scope)
    if prior is not None and record is None:
        raise StorageFault("storage_schema_unsupported")
    if record is None:
        if authority.state not in {AuthorityState.APPROVED, AuthorityState.CONSUMED}:
            raise ReviewFault("execution_conflict")
        completed: ExecutionStopTerminal | None = None
        if authority.state == AuthorityState.CONSUMED and authority.claim_state != "claimed":
            if authority.claim_state != "finished":
                raise ReviewFault("execution_outcome_unknown")
            completed = self._result_stop_terminal(
                self._execution_result(
                    connection,
                    DockerLaunchScope(
                        project_id=authority.project_id,
                        canonical_root=authority.canonical_root,
                        claim_id=cast(UUID, authority.claim_id),
                    ),
                )
            )
        now, _, healthy = self._review_time()
        stamp = (
            now if type(now) is int and 0 <= now <= INT64_MAX - 15000 else authority.created_at_ms
        )
        record = ExecutionStopRecord(
            scope=scope,
            request_id=request.request_id,
            manifest_digest=authority.manifest_digest,
            reason=request.reason,
            requested_at_ms=max(authority.created_at_ms, stamp),
            clock_confirmed=healthy,
            terminal=ExecutionStopTerminal(
                outcome="timed_out" if request.reason == "timeout" else "cancelled",
                evidence="not_started",
                cleanup_verified=True,
            )
            if authority.state == AuthorityState.APPROVED
            else completed,
        )
        if authority.state == AuthorityState.APPROVED:
            connection.execute(
                "UPDATE execution_authorities SET state='invalidated' WHERE proposal_id=?",
                (str(authority.proposal_id),),
            )
        self._write_stop(connection, authority, record)
    if prior is None:
        connection.execute(
            "INSERT INTO execution_authority_operations VALUES (?,?,?)",
            (str(request.request_id), digest, str(authority.proposal_id)),
        )
    self._commit_write(connection, fd)
    return record


@_method
def _finish_stop(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    observed: ExecutionStopObservation,
    root: int,
    fd: int,
    *,
    capture: DockerOutputSnapshot | None = None,
) -> ExecutionStopRecord:
    if capture is not None:
        ExecutionStopCapture(observation=observed, output=capture)
    authority = self._stop_target(connection, observed.scope)
    self._begin_write(connection, root, fd)
    record = self._read_stop(connection, authority, observed.scope)
    if (
        record is None
        or observed.request_id != record.request_id
        or observed.manifest_digest != record.manifest_digest
    ):
        raise ReviewFault("execution_target_mismatch")
    if observed.terminal.outcome == "completed":
        # Only the unchanged ordinary completion port can establish v1
        # completion. A stop observation cannot manufacture its missing row.
        if (
            connection.execute(
                "SELECT 1 FROM execution_results r JOIN execution_launches l "
                "ON l.attempt_id=r.attempt_id WHERE l.claim_id=?",
                (str(authority.claim_id),),
            ).fetchone()
            is None
        ):
            raise ReviewFault("execution_conflict")
        legacy = self._execution_result(
            connection,
            DockerLaunchScope(
                project_id=authority.project_id,
                canonical_root=authority.canonical_root,
                claim_id=cast(UUID, authority.claim_id),
            ),
        )
        if (
            legacy.disposition != "completed"
            or self._result_stop_terminal(legacy) != observed.terminal
        ):
            raise ReviewFault("execution_conflict")
    if record.terminal is not None:
        if record.terminal != observed.terminal:
            raise ReviewFault("execution_conflict")
        if capture is not None:
            view, _ = self._stop_payload(connection, authority, record)
            if view.payload_state == "missing":
                raise ReviewFault("execution_conflict")
        self._commit_write(connection, fd)
        return record
    terminal = observed.terminal
    if (
        authority.state != AuthorityState.CONSUMED
        or authority.claim_state not in {"claimed", "unknown"}
        or (authority.claim_state == "unknown" and terminal.outcome != "unknown")
    ):
        raise ReviewFault("execution_outcome_unknown")
    if terminal.evidence == "not_started":
        if connection.execute(
            "SELECT 1 FROM execution_launches WHERE claim_id=?", (str(authority.claim_id),)
        ).fetchone():
            raise ReviewFault("execution_outcome_unknown")
        # Positive no-launch evidence is the committed stop fence plus the
        # mandatory journal-before-mutation rule, not a missing row alone.
    elif terminal.evidence == "owned":
        owned = observed.owned
        assert owned is not None
        launch = self._launch_status(
            connection,
            DockerLaunchScope(
                project_id=authority.project_id,
                canonical_root=authority.canonical_root,
                claim_id=cast(UUID, authority.claim_id),
            ),
        )
        if (
            launch.intent != owned.intent
            or launch.container_id != owned.container_id
            or launch.created_at != owned.created_at
            or terminal.exit_code != launch.exit_code
        ):
            raise ReviewFault("execution_target_mismatch")
        if terminal.outcome != "unknown" and (
            launch.uncertain
            or "cleaned" not in launch.phases
            or ("start_intent" in launch.phases and "exited" not in launch.phases)
        ):
            raise ReviewFault("execution_outcome_unknown")
        if terminal.output_facts is not None and (
            terminal.output_facts.retention_limit != authority.manifest.profile.output_bytes
        ):
            raise ReviewFault("execution_target_mismatch")
    elif terminal.outcome != "unknown":
        raise ReviewFault("execution_outcome_unknown")
    try:
        updated = ExecutionStopRecord.model_validate(
            {
                **record.model_dump(),
                "terminal": terminal.model_dump(),
            }
        )
    except ValueError:
        raise ReviewFault("execution_conflict") from None
    self._write_stop(connection, authority, updated)
    if capture is not None:
        self._insert_stop_payload(connection, authority, updated, capture)
    connection.execute(
        "UPDATE execution_authorities SET claim_state=? WHERE proposal_id=?",
        (
            "unknown" if terminal.outcome == "unknown" else "finished",
            str(authority.proposal_id),
        ),
    )
    self._commit_write(connection, fd)
    return updated
