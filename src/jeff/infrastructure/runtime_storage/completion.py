"""One durable completion transaction and its original lifecycle reconciliation."""

import json
import sqlite3
from hashlib import sha256
from typing import TYPE_CHECKING

from jeff.application.execution import (
    DockerBackendFault,
    DockerCompletion,
    DockerExecutionResult,
    DockerLaunchEvent,
    DockerLaunchScope,
    ExecutionInspectionScope,
    ExecutionStopRecord,
)
from jeff.domain.execution import fingerprint

from .binding import _method
from .catalog import _AUTHORITY_COLUMNS

if TYPE_CHECKING:
    from .store import SQLiteStorage


@_method
def _complete_execution(
    self: "SQLiteStorage",
    connection: sqlite3.Connection,
    completion: DockerCompletion,
    root: int,
    fd: int,
) -> DockerExecutionResult:
    owned = completion.owned
    intent = owned.intent
    supplied = intent.record
    if self.instance_id is None or supplied.owner_instance_id != self.instance_id:
        raise DockerBackendFault("docker_authority_required")
    scope = DockerLaunchScope.model_validate(
        {
            "project_id": supplied.project_id,
            "canonical_root": supplied.canonical_root,
            "claim_id": supplied.claim_id,
        }
    )
    observed = self._launch_status(connection, scope)
    authority_row = connection.execute(
        "SELECT " + _AUTHORITY_COLUMNS + " FROM execution_authorities WHERE claim_id=?",
        (str(supplied.claim_id),),
    ).fetchone()
    if authority_row is None:
        raise DockerBackendFault("docker_authority_required")
    current = self._authority_record(authority_row)
    if (
        current.model_dump(exclude={"claim_state"}) != supplied.model_dump(exclude={"claim_state"})
        or observed.intent != intent
        or observed.container_id != owned.container_id
        or observed.created_at != owned.created_at
        or observed.claim_state not in {"claimed", "unknown"}
        or completion.output.retention_limit != supplied.manifest.profile.output_bytes
        or (
            completion.outcome.exit_code is not None
            and observed.exit_code is not None
            and completion.outcome.exit_code != observed.exit_code
        )
        or connection.execute(
            "SELECT 1 FROM projects WHERE project_id=? AND canonical_root=?",
            (str(supplied.project_id), str(supplied.canonical_root)),
        ).fetchone()
        is None
        or connection.execute(
            "SELECT 1 FROM execution_results WHERE attempt_id=?", (str(intent.attempt_id),)
        ).fetchone()
        is not None
    ):
        raise DockerBackendFault("docker_authority_required")
    if completion.confirmed and (
        observed.uncertain
        or observed.phases != ("create_intent", "created", "start_intent", "exited", "cleaned")
        or observed.exit_code != completion.outcome.exit_code
    ):
        raise DockerBackendFault("docker_mutation_unconfirmed")
    now = self._sample_retention_wall()
    if now is None:
        raise DockerBackendFault("docker_mutation_unconfirmed")
    cutoff = max(0, now - (self._retention_days or 30) * 86400000)
    if not completion.confirmed and any(
        r[2] == "quarantined" and (not r[4] or r[3] >= cutoff)
        for r in self._retained_payloads(connection)
    ):
        raise DockerBackendFault("docker_busy")
    metadata = json.dumps(
        {
            "outcome": completion.outcome.model_dump(mode="json"),
            "output_facts": completion.output_facts.model_dump(mode="json"),
            "output_digest": completion.output_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(metadata.encode("utf-8")) > 8192:
        raise DockerBackendFault("docker_output_failed")
    self._begin_write(connection, root, fd)
    self._retire_execution_payloads(connection, cutoff, age_limit=1)
    if not completion.confirmed and "unknown" not in observed.phases:
        event = DockerLaunchEvent(
            intent=intent,
            phase="unknown",
            container_id=owned.container_id,
            created_at=owned.created_at,
        )
        digest = fingerprint(
            {"intent": intent.fingerprint, **event.model_dump(mode="json", exclude={"intent"})}
        )
        connection.execute(
            "INSERT INTO execution_launch_events VALUES (?,?,?,?)",
            (str(intent.attempt_id), "unknown", len(observed.phases) + 1, digest),
        )
    connection.execute(
        "INSERT INTO execution_results (attempt_id,disposition,recorded_at_ms,metadata_json,"
        "completion_digest,stdout,stderr) VALUES (?,?,?,?,?,?,?)",
        (
            str(intent.attempt_id),
            "completed" if completion.confirmed else "quarantined",
            now,
            metadata,
            completion.fingerprint,
            completion.output.stdout,
            completion.output.stderr,
        ),
    )
    if (pytest_result := completion.pytest_result) is not None:
        report_json = (
            pytest_result.report.model_dump_json() if pytest_result.report is not None else None
        )
        report_digest = (
            sha256((completion.fingerprint + report_json).encode()).hexdigest()
            if report_json is not None
            else None
        )
        connection.execute(
            "INSERT INTO execution_pytest_reports VALUES (?,?,?,?)",
            (str(intent.attempt_id), pytest_result.status, report_json, report_digest),
        )
    connection.execute(
        "UPDATE execution_authorities SET claim_state=? WHERE claim_id=?",
        ("finished" if completion.confirmed else "unknown", str(supplied.claim_id)),
    )
    self._retire_execution_payloads(connection, cutoff, age_limit=0)
    stop_scope = ExecutionInspectionScope(
        project_id=supplied.project_id,
        canonical_root=supplied.canonical_root,
        owner_instance_id=supplied.owner_instance_id,
        approval_id=supplied.approval_id,
    )
    stop = self._read_stop(connection, supplied, stop_scope)
    if stop is not None and stop.terminal is None:
        # Preserve the original immutable result; cancellation cannot relabel
        # an ordinary completion that won before termination was established.
        terminal = self._result_stop_terminal(self._execution_result(connection, scope))
        updated = ExecutionStopRecord.model_validate(
            {
                **stop.model_dump(),
                "terminal": terminal.model_dump(),
            }
        )
        self._write_stop(connection, supplied, updated)
    self._commit_write(connection, fd)
    return self._execution_result(connection, scope)
