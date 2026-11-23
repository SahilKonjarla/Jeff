"""Proof-only M03 terminal facts and bounded sanitized output on the PostgreSQL writer."""

from __future__ import annotations

import time
from collections.abc import Callable, Set
from contextlib import AbstractContextManager
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.execution import (
    AuthorityRecord,
    DockerBackendFault,
    DockerCompletion,
    DockerExecutionResult,
    DockerLaunchScope,
    DockerOutputSnapshot,
    DockerOwnedContainer,
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

from . import postgres_execution as execution
from . import postgres_payloads as payloads

if TYPE_CHECKING:
    from .postgres_storage import Connection

RESULT_DDL = (
    "CREATE TABLE jeff_proof.results (approval_id uuid PRIMARY KEY "
    "REFERENCES jeff_proof.authorities(approval_id), sequence bigint GENERATED ALWAYS AS IDENTITY, "
    "facts jsonb NOT NULL)",
    "CREATE TABLE jeff_proof.stops (approval_id uuid PRIMARY KEY "
    "REFERENCES jeff_proof.authorities(approval_id), facts jsonb NOT NULL)",
    *payloads.OUTPUT_DDL,
)


def _now() -> int:
    return time.time_ns() // 1_000_000


def _ownership(owned: DockerOwnedContainer) -> str:
    return fingerprint(
        dict(intent=owned.intent.fingerprint, container=owned.container_id, birth=owned.created_at)
    )


def _scope(record: AuthorityRecord) -> DockerLaunchScope:
    if record.claim_id is None:
        raise ReviewFault("execution_conflict")
    return DockerLaunchScope(
        project_id=record.project_id, canonical_root=record.canonical_root, claim_id=record.claim_id
    )


def _read_result(connection: Connection, approval: UUID) -> DockerExecutionResult:
    row = connection.execute(
        "SELECT r.sequence,r.facts,o.payload,o.retirement_reason,o.report_text,o.report_digest "
        "FROM jeff_proof.results r "
        "JOIN jeff_proof.output o USING (approval_id) WHERE approval_id=%s",
        (approval,),
    ).fetchone()
    if row is None:
        raise ReviewFault("execution_not_found")
    sequence, facts, output, reason, report, report_digest = row
    if not isinstance(facts, dict):
        raise StorageFault("storage_unrecognized")
    result = DockerExecutionResult.model_validate(
        facts
        | dict(
            sequence=sequence,
            output=output,
            retirement_reason=reason,
            payload_state="retained" if output is not None else "retired",
        )
    )
    return result.model_copy(
        update={"pytest_result": payloads.decode_report(result, report, report_digest)}
    )


def _read_stop(connection: Connection, approval: UUID) -> ExecutionStopRecord | None:
    row = connection.execute(
        "SELECT facts FROM jeff_proof.stops WHERE approval_id=%s", (approval,)
    ).fetchone()
    return None if row is None else ExecutionStopRecord.model_validate(row[0])


def _save_stop(connection: Connection, approval: UUID, record: ExecutionStopRecord) -> None:
    connection.execute(
        "INSERT INTO jeff_proof.stops VALUES (%s,%s::jsonb) "
        "ON CONFLICT (approval_id) DO UPDATE SET facts=EXCLUDED.facts",
        (approval, record.model_dump_json()),
    )


def _completed_terminal(result: DockerExecutionResult) -> ExecutionStopTerminal:
    return ExecutionStopTerminal(
        outcome="completed",
        evidence="owned",
        cleanup_verified=True,
        exit_code=result.outcome.exit_code,
        attempt_id=result.owned.intent.attempt_id,
        ownership_digest=_ownership(result.owned),
        output_facts=result.output_facts,
        output_digest=result.output_digest,
    )


class PostgresExecutionResults:
    """Shares the registry/authority writer lease; does not act on a container."""

    def __init__(
        self,
        transaction: Callable[[float, bool], AbstractContextManager[Connection]],
        owner: UUID,
        confirmed_claims: Set[UUID],
    ) -> None:
        self._transaction = transaction
        self._owner = owner
        self._claims = confirmed_claims

    def complete_execution(
        self, completion: DockerCompletion, *, deadline: float
    ) -> DockerExecutionResult:
        owned = completion.owned
        original = owned.intent.record
        if original.owner_instance_id != self._owner or original.claim_id not in self._claims:
            raise DockerBackendFault("docker_authority_required")
        with self._transaction(deadline, True) as connection:
            current = execution.find_authority(connection, original.approval_id)
            launch = execution.launch_status(connection, _scope(current))
            if (
                current.claim_state not in {"claimed", "unknown"}
                or launch.intent != owned.intent
                or launch.container_id != owned.container_id
                or launch.created_at != owned.created_at
                or (
                    completion.outcome.exit_code is not None
                    and launch.exit_code is not None
                    and launch.exit_code != completion.outcome.exit_code
                )
                or completion.output.retention_limit != current.manifest.profile.output_bytes
                or connection.execute(
                    "SELECT 1 FROM jeff_proof.results WHERE approval_id=%s", (current.approval_id,)
                ).fetchone()
                is not None
            ):
                raise DockerBackendFault("docker_authority_required")
            if completion.confirmed and (
                launch.uncertain
                or launch.exit_code != completion.outcome.exit_code
                or launch.phases
                != ("create_intent", "created", "start_intent", "exited", "cleaned")
            ):
                raise DockerBackendFault("docker_mutation_unconfirmed")
            stop = _read_stop(connection, current.approval_id)
            if stop is not None and stop.terminal is not None:
                raise DockerBackendFault("docker_authority_required")
            stamp = _now()
            report = payloads.encode_report(completion)
            result = DockerExecutionResult(
                owned=owned,
                outcome=completion.outcome,
                output_facts=completion.output_facts,
                output_digest=completion.output_digest,
                completion_digest=completion.fingerprint,
                disposition="completed" if completion.confirmed else "quarantined",
                sequence=1,
                recorded_at_ms=stamp,
                payload_state="retained",
                output=completion.output,
            )
            connection.execute(
                "INSERT INTO jeff_proof.results (approval_id,facts) VALUES (%s,%s::jsonb)",
                (
                    current.approval_id,
                    result.model_dump_json(exclude={"sequence", "output", "pytest_result"}),
                ),
            )
            payloads.archive(
                connection,
                current.approval_id,
                completion.output,
                stamp,
                quarantined=not completion.confirmed,
                report=report,
            )
            execution.save_authority(
                connection,
                AuthorityRecord.model_validate(
                    current.model_dump()
                    | {"claim_state": "finished" if completion.confirmed else "unknown"}
                ),
            )
            if stop is not None:
                terminal = (
                    _completed_terminal(result)
                    if completion.confirmed
                    else ExecutionStopTerminal(
                        outcome="unknown", evidence="uncertain", cleanup_verified=False
                    )
                )
                _save_stop(
                    connection, current.approval_id, stop.model_copy(update={"terminal": terminal})
                )
            return _read_result(connection, current.approval_id)

    def execution_result(
        self, scope: DockerLaunchScope, *, deadline: float
    ) -> DockerExecutionResult:
        with self._transaction(deadline, False) as connection:
            record = execution.find_authority(connection, scope.claim_id, by_claim=True)
            if (record.project_id, record.canonical_root) != (
                scope.project_id,
                scope.canonical_root,
            ):
                raise ReviewFault("execution_not_found")
            return _read_result(connection, record.approval_id)

    def _target(self, connection: Connection, scope: ExecutionInspectionScope) -> AuthorityRecord:
        if scope.approval_id is None or scope.owner_instance_id != self._owner:
            raise ReviewFault("execution_not_found")
        record = execution.find_authority(connection, scope.approval_id)
        if (record.project_id, record.canonical_root) != (scope.project_id, scope.canonical_root):
            raise ReviewFault("execution_not_found")
        return record

    def request_stop(
        self, request: ExecutionStopRequest, *, deadline: float
    ) -> ExecutionStopRecord:
        with self._transaction(deadline, True) as connection:
            record = self._target(connection, request)
            if (
                record.owner_instance_id != self._owner
                or record.manifest_digest != request.manifest_digest
            ):
                raise ReviewFault("execution_target_mismatch")
            prior = _read_stop(connection, record.approval_id)
            digest = fingerprint(request.model_dump(mode="json"))
            seen = connection.execute(
                "SELECT digest,approval_id FROM jeff_proof.authority_ops WHERE request_id=%s",
                (request.request_id,),
            ).fetchone()
            if seen is not None and (seen != (digest, record.approval_id) or prior is None):
                raise ReviewFault("execution_conflict")
            if prior is None:
                if record.state == AuthorityState.APPROVED:
                    terminal = ExecutionStopTerminal(
                        outcome="timed_out" if request.reason == "timeout" else "cancelled",
                        evidence="not_started",
                        cleanup_verified=True,
                    )
                    execution.save_authority(
                        connection, record.model_copy(update={"state": AuthorityState.INVALIDATED})
                    )
                elif record.claim_state == "finished":
                    terminal = _completed_terminal(_read_result(connection, record.approval_id))
                elif record.claim_state == "claimed" and record.claim_id in self._claims:
                    terminal = None
                else:
                    raise ReviewFault("execution_outcome_unknown")
                prior = ExecutionStopRecord(
                    scope=ExecutionInspectionScope(
                        project_id=record.project_id,
                        canonical_root=record.canonical_root,
                        approval_id=record.approval_id,
                        owner_instance_id=self._owner,
                    ),
                    request_id=request.request_id,
                    manifest_digest=request.manifest_digest,
                    reason=request.reason,
                    requested_at_ms=max(_now(), record.created_at_ms),
                    clock_confirmed=True,
                    terminal=terminal,
                )
                _save_stop(connection, record.approval_id, prior)
            if seen is None:
                connection.execute(
                    "INSERT INTO jeff_proof.authority_ops VALUES (%s,%s,%s)",
                    (request.request_id, digest, record.approval_id),
                )
            return prior

    def finish_stop(
        self,
        observed: ExecutionStopObservation,
        *,
        deadline: float,
        capture: DockerOutputSnapshot | None = None,
    ) -> ExecutionStopRecord:
        if capture is not None:
            ExecutionStopCapture(observation=observed, output=capture)
        with self._transaction(deadline, True) as connection:
            record = self._target(connection, observed.scope)
            prior = _read_stop(connection, record.approval_id)
            if (
                record.owner_instance_id != self._owner
                or prior is None
                or prior.request_id != observed.request_id
                or prior.manifest_digest != observed.manifest_digest
            ):
                raise ReviewFault("execution_target_mismatch")
            terminal = observed.terminal
            if prior.terminal is not None:
                if prior.terminal != terminal or capture is not None:
                    raise ReviewFault("execution_conflict")
                return prior
            if (
                terminal.outcome == "completed"
                or record.claim_id not in self._claims
                or record.claim_state not in {"claimed", "unknown"}
                or (record.claim_state == "unknown" and terminal.outcome != "unknown")
            ):
                raise ReviewFault("execution_outcome_unknown")
            if terminal.evidence == "not_started":
                if (
                    connection.execute(
                        "SELECT 1 FROM jeff_proof.launches WHERE claim_id=%s", (record.claim_id,)
                    ).fetchone()
                    is not None
                ):
                    raise ReviewFault("execution_outcome_unknown")
            elif observed.owned is not None:
                launch = execution.launch_status(connection, _scope(record))
                if (launch.intent, launch.container_id, launch.created_at, launch.exit_code) != (
                    observed.owned.intent,
                    observed.owned.container_id,
                    observed.owned.created_at,
                    terminal.exit_code,
                ):
                    raise ReviewFault("execution_target_mismatch")
                if terminal.outcome != "unknown" and (
                    launch.uncertain or "cleaned" not in launch.phases
                ):
                    raise ReviewFault("execution_outcome_unknown")
            if (
                terminal.output_facts is not None
                and terminal.output_facts.retention_limit != record.manifest.profile.output_bytes
            ):
                raise ReviewFault("execution_target_mismatch")
            result = ExecutionStopRecord.model_validate(prior.model_dump() | {"terminal": terminal})
            if capture is not None:
                payloads.archive(
                    connection,
                    record.approval_id,
                    capture,
                    _now(),
                    quarantined=terminal.outcome == "unknown",
                )
            _save_stop(connection, record.approval_id, result)
            execution.save_authority(
                connection,
                record.model_copy(
                    update={
                        "claim_state": "unknown" if terminal.outcome == "unknown" else "finished"
                    }
                ),
            )
            return result

    def inspect_stop(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionStopRecord | None:
        with self._transaction(deadline, False) as connection:
            return _read_stop(connection, self._target(connection, scope).approval_id)

    def stop_output(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> DockerOutputSnapshot | None:
        with self._transaction(deadline, False) as connection:
            target = self._target(connection, scope)
            stop = _read_stop(connection, target.approval_id)
            row = connection.execute(
                "SELECT payload FROM jeff_proof.output WHERE approval_id=%s", (target.approval_id,)
            ).fetchone()
            if stop is None or stop.terminal is None or row is None or row[0] is None:
                return None
            output = DockerOutputSnapshot.model_validate(row[0])
            if sha256(output.model_dump_json().encode()).hexdigest() != stop.terminal.output_digest:
                raise StorageFault("storage_unrecognized")
            return output

    def retire_output_before(self, cutoff: int, *, deadline: float) -> int:
        if type(cutoff) is not int or not 0 <= cutoff <= min(_now(), INT64_MAX):
            raise StorageFault("storage_busy")
        with self._transaction(deadline, True) as connection:
            return payloads.retire(connection, cutoff)
