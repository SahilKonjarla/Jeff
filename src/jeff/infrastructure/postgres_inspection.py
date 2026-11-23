"""Scoped historical lookup over the PostgreSQL proof schema, without launch authority."""

import time
from hashlib import sha256
from typing import Literal

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityRecord,
    DockerOutputSnapshot,
    ExecutionInspection,
    ExecutionInspectionScope,
    ExecutionListScope,
    ExecutionStopView,
    FreshExecutionPage,
    FreshExecutionView,
    ProposalRequestScope,
    ReviewFault,
)
from jeff.application.execution.pytest_report import PYTEST_RUNNER_ARGV, pytest_run_token
from jeff.application.execution.requests import parse_execution_cursor
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState, fingerprint

from .postgres_results import _read_result, _read_stop
from .postgres_storage import Connection


def lookup_proposal(connection: Connection, scope: ProposalRequestScope) -> AuthorityRecord | None:
    row = connection.execute(
        "SELECT a.payload,o.digest FROM jeff_proof.authority_ops o "
        "JOIN jeff_proof.authorities a USING (approval_id) WHERE o.request_id=%s",
        (scope.request_id,),
    ).fetchone()
    if row is None:
        return None
    record = AuthorityRecord.model_validate(row[0])
    expected = AuthorityOperation(
        operation="propose",
        request_id=scope.request_id,
        project_id=scope.project_id,
        canonical_root=scope.canonical_root,
        owner_instance_id=scope.owner_instance_id,
        manifest=record.manifest,
        current_manifest_digest=record.manifest_digest,
    )
    if (record.project_id, record.canonical_root, record.owner_instance_id) != (
        scope.project_id,
        scope.canonical_root,
        scope.owner_instance_id,
    ) or fingerprint(expected.model_dump(mode="json")) != row[1]:
        raise ReviewFault("execution_conflict")
    return record


def list_executions(connection: Connection, scope: ExecutionListScope) -> FreshExecutionPage:
    where = (
        " FROM jeff_proof.authorities WHERE payload->>'project_id'=%s "
        "AND payload->>'canonical_root'=%s"
    )
    stamp = "(payload->>'created_at_ms')::bigint"
    key = "(payload->>'proposal_id')"
    scope_args = (str(scope.project_id), str(scope.canonical_root))
    args: tuple[object, ...] = scope_args
    if scope.after is not None:
        millis, proposal = parse_execution_cursor(scope.after)
        if (
            connection.execute(
                "SELECT 1" + where + f" AND {stamp}=%s AND {key}=%s",
                (*scope_args, millis, proposal),
            ).fetchone()
            is None
        ):
            raise ReviewFault("execution_conflict")
        where += f" AND ({stamp},{key})<(%s,%s)"
        args += (millis, proposal)
    if scope.claim_filter is not None:
        where += " AND payload->>'claim_state'=%s"
        args += (scope.claim_filter,)
    if scope.kind_filter is not None:
        prefix = " AND ".join(f"payload#>>'{{manifest,original,argv,{i}}}'=%s" for i in range(4))
        where += (
            " AND COALESCE(payload#>>'{manifest,authority_kind}'='guarded_execution_v1' AND "
            + prefix
            + " AND payload#>>'{manifest,original,argv,4}' ~ '^[a-f0-9]{32}$', false)=%s"
        )
        args += (*PYTEST_RUNNER_ARGV, scope.kind_filter == "pytest")
    rows = connection.execute(
        "SELECT payload" + where + f" ORDER BY {stamp} DESC,{key} DESC LIMIT %s",
        (*args, scope.limit + 1),
    ).fetchall()
    items = []
    now = time.time_ns() // 1_000_000
    for (raw,) in rows[: scope.limit]:
        record = AuthorityRecord.model_validate(raw)
        if (
            record.state in {AuthorityState.PENDING, AuthorityState.APPROVED}
            and now >= record.expires_at_ms
        ):
            record = record.model_copy(update={"state": AuthorityState.EXPIRED})
        items.append(FreshExecutionView.from_record(record))
    after = None
    if items and len(rows) > scope.limit:
        last = items[-1]
        after = f"{last.created_at_ms}:{last.proposal_id}"
    return FreshExecutionPage(
        project_id=scope.project_id,
        canonical_root=scope.canonical_root,
        items=tuple(items),
        next_after=after,
    )


def _archive_clock(
    stamp: int, retention_days: int
) -> Literal["expired", "clock_unconfirmed"] | None:
    now = time.time_ns() // 1_000_000
    if now < stamp:
        return "clock_unconfirmed"
    return "expired" if stamp < now - retention_days * 86_400_000 else None


def inspect(
    connection: Connection, scope: ExecutionInspectionScope, *, retention_days: int
) -> ExecutionInspection:
    base = (
        "SELECT payload FROM jeff_proof.authorities WHERE payload->>'project_id'=%s "
        "AND payload->>'canonical_root'=%s "
    )
    params: tuple[object, ...] = (str(scope.project_id), str(scope.canonical_root))
    if scope.approval_id is not None:
        clause = "AND approval_id=%s"
        params += (scope.approval_id,)
    elif scope.job_id is not None:
        clause = "AND payload->>'job_id'=%s"
        params += (str(scope.job_id),)
    else:
        clause = (
            "AND payload#>>'{manifest,authority_kind}'='guarded_execution_v1' "
            "AND payload#>>'{manifest,original,argv,0}'=%s "
            "AND payload#>>'{manifest,original,argv,1}'=%s "
            "AND payload#>>'{manifest,original,argv,2}'=%s "
            "AND payload#>>'{manifest,original,argv,3}'=%s "
            "AND payload#>>'{manifest,original,argv,4}' ~ '^[a-f0-9]{32}$' "
            "ORDER BY (payload->>'created_at_ms')::bigint DESC, "
            "payload->>'proposal_id' DESC LIMIT 1"
        )
        params += PYTEST_RUNNER_ARGV
    row = connection.execute(base + clause, params).fetchone()
    if row is None:
        raise ReviewFault("execution_not_found")
    record = AuthorityRecord.model_validate(row[0])
    if scope.latest_pytest and pytest_run_token(record.manifest.argv) is None:
        raise StorageFault("storage_unrecognized")
    approval = record.approval_id
    stop = _read_stop(connection, approval)
    has_result = (
        connection.execute(
            "SELECT 1 FROM jeff_proof.results WHERE approval_id=%s", (approval,)
        ).fetchone()
        is not None
    )
    if has_result:
        result = _read_result(connection, approval)
        hidden = _archive_clock(result.recorded_at_ms, retention_days)
        if hidden is not None and result.output is not None:
            result = result.model_copy(
                update={"output": None, "pytest_result": None, "payload_state": hidden}
            )
        view = (
            None
            if stop is None
            else ExecutionStopView.from_record(
                stop,
                payload_state=result.payload_state,
                retirement_reason=result.retirement_reason,
                recorded_at_ms=result.recorded_at_ms,
            )
        )
        return ExecutionInspection(
            record=record, completion_state="recorded", result=result, stop=view
        )
    if stop is not None and stop.terminal is not None:
        row = connection.execute(
            "SELECT payload,recorded_at_ms,retirement_reason FROM jeff_proof.output "
            "WHERE approval_id=%s",
            (approval,),
        ).fetchone()
        output = None
        if row is None:
            view = ExecutionStopView.from_record(stop, payload_state="missing")
        else:
            payload, stamp, reason = row
            if type(stamp) is not int:
                raise StorageFault("storage_unrecognized")
            hidden = (
                _archive_clock(stamp, retention_days)
                if stop.clock_confirmed
                else "clock_unconfirmed"
            )
            if payload is not None and hidden is None:
                output = DockerOutputSnapshot.model_validate(payload)
                if (
                    sha256(output.model_dump_json().encode()).hexdigest()
                    != stop.terminal.output_digest
                ):
                    raise StorageFault("storage_unrecognized")
            view = ExecutionStopView.from_record(
                stop,
                payload_state="retired" if payload is None else hidden or "retained",
                retirement_reason=reason,
                recorded_at_ms=stamp,
            )
        return ExecutionInspection(
            record=record, completion_state="stop_recorded", stop=view, stop_output=output
        )
    state = "not_claimed"
    if record.claim_id is not None:
        state = (
            "launch_without_result"
            if connection.execute(
                "SELECT 1 FROM jeff_proof.launches WHERE claim_id=%s", (record.claim_id,)
            ).fetchone()
            is not None
            else "claim_only"
        )
    return ExecutionInspection.model_validate(
        dict(
            record=record,
            completion_state=state,
            stop=None if stop is None else ExecutionStopView.from_record(stop),
        )
    )
