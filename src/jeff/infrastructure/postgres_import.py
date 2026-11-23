"""One-way, offline DB12 import into an empty PostgreSQL proof schema."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from jeff.application.execution import (
    AuthorityRecord,
    DockerExecutionResult,
    DockerLaunchIntent,
    DockerLaunchSnapshot,
    DockerOutputSnapshot,
    DockerOwnedContainer,
    ExecutionStopRecord,
)
from jeff.application.execution.pytest_report import PytestReport, PytestResult
from jeff.application.execution.watch import notification_id_for
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState

from .postgres_storage import Connection, PostgresStorage
from .runtime_storage import authority, catalog, events, security


def import_db12(source: Path, destination: PostgresStorage, *, deadline: float) -> dict[str, int]:
    """The caller supplies an offline copy and holds the destination writer lease."""
    if (
        not source.is_absolute()
        or not source.is_file()
        or source.is_symlink()
        or any(
            source.with_name(source.name + suffix).exists()
            for suffix in ("-journal", "-wal", "-shm")
        )
    ):
        raise StorageFault("storage_unsafe")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        security._guard(db)
        db.execute("BEGIN")  # All checks and reads see one source snapshot.
        if security._schema(db) != 12 or db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise StorageFault("storage_schema_unsupported")
        with destination._transaction(deadline, write=True) as pg:
            # Unqualified destination names are never accepted from the caller.
            for table in (
                "projects",
                "receipts",
                "events",
                "tombstones",
                "authorities",
                "authority_ops",
                "launches",
                "launch_events",
                "results",
                "output",
                "stops",
                "watches",
            ):
                if pg.execute(f"SELECT 1 FROM jeff_proof.{table} LIMIT 1").fetchone() is not None:
                    raise StorageFault("storage_unrecognized")
            counts = _import_project_data(db, pg)
            counts.update(_import_execution(db, pg))
            counts.update(_archive_legacy(db, pg))
            return counts


def _copy(db: sqlite3.Connection, pg: Connection, select: str, insert: str) -> int:
    count = 0
    for row in db.execute(select):
        pg.execute(insert, row)
        count += 1
    return count


def _import_project_data(db: sqlite3.Connection, pg: Connection) -> dict[str, int]:
    counts = {
        "projects": _copy(
            db,
            pg,
            "SELECT project_id,canonical_root FROM projects",
            "INSERT INTO jeff_proof.projects VALUES (%s,%s)",
        ),
        "tombstones": _copy(
            db,
            pg,
            "SELECT request_id FROM request_tombstones",
            "INSERT INTO jeff_proof.tombstones VALUES (%s)",
        ),
        "receipts": 0,
        "events": 0,
    }
    facts = events._activity_meta(db)
    pg.execute(
        "UPDATE jeff_proof.activity SET database_epoch=%s,coverage_started_at_ms=%s,"
        "high_water=%s,retention_generation=%s,replay_floor=%s WHERE singleton",
        (
            facts.database_epoch,
            facts.coverage_started_at_ms,
            facts.high_water,
            facts.retention_generation,
            facts.replay_floor,
        ),
    )
    for row in db.execute(f"SELECT {catalog._COLUMNS} FROM request_receipts"):
        receipt = events._receipt(row)
        if receipt.state in {"accepted", "running"}:
            raise StorageFault("storage_recovery_required")  # Reconcile source first.
        pg.execute(
            "INSERT INTO jeff_proof.receipts VALUES (%s,%s::jsonb)",
            (receipt.request_id, receipt.model_dump_json()),
        )
        counts["receipts"] += 1
    for row in db.execute(f"SELECT {catalog._EVENT_COLUMNS} FROM activity_events"):
        event = events._activity_event(row)
        pg.execute(
            "INSERT INTO jeff_proof.events VALUES (%s,%s,%s::jsonb)",
            (event.sequence, event.request_id, event.model_dump_json()),
        )
        counts["events"] += 1
    return counts


def _import_execution(db: sqlite3.Connection, pg: Connection) -> dict[str, int]:
    counts = dict(authorities=0, launches=0, results=0, reports=0, stops=0, watches=0)
    for row in db.execute(f"SELECT {catalog._AUTHORITY_COLUMNS} FROM execution_authorities"):
        record = authority._authority_record(row)
        update: dict[str, object] = {}
        if record.state in {AuthorityState.PENDING, AuthorityState.APPROVED}:
            update["state"] = AuthorityState.INVALIDATED
        if record.claim_state == "claimed":
            update["claim_state"] = "unknown"
        record = AuthorityRecord.model_validate(record.model_dump() | update)
        pg.execute(
            "INSERT INTO jeff_proof.authorities VALUES (%s,%s,%s::jsonb)",
            (record.approval_id, record.claim_id, record.model_dump_json()),
        )
        counts["authorities"] += 1
    counts["authority_requests"] = _copy(
        db,
        pg,
        "SELECT o.request_id,o.payload_digest,a.approval_id "
        "FROM execution_authority_operations o JOIN execution_authorities a USING (proposal_id)",
        "INSERT INTO jeff_proof.authority_ops VALUES (%s,%s,%s)",
    )
    for row in db.execute(
        "SELECT intent_json,intent_digest,l.container_id,l.created_at,l.observed_container_id,"
        "l.exit_code,a.claim_state FROM execution_launches l "
        "JOIN execution_authorities a USING (claim_id)"
    ):
        intent = DockerLaunchIntent.model_validate_json(row[0])
        if intent.fingerprint != row[1]:
            raise StorageFault("storage_unrecognized")
        launch_rows = db.execute(
            "SELECT phase,event_digest FROM execution_launch_events WHERE attempt_id=? "
            "ORDER BY sequence",
            (str(intent.attempt_id),),
        ).fetchall()
        phases = tuple(phase for phase, _ in launch_rows)
        snapshot = DockerLaunchSnapshot.model_validate(
            dict(
                intent=intent,
                phases=phases,
                container_id=row[2],
                created_at=row[3],
                observed_container_id=row[4],
                exit_code=row[5],
                claim_state=row[6],
                uncertain=row[6] == "unknown" or "unknown" in phases,
            )
        )
        pg.execute(
            "INSERT INTO jeff_proof.launches VALUES (%s,%s,%s,%s::jsonb)",
            (intent.attempt_id, intent.record.claim_id, row[1], snapshot.model_dump_json()),
        )
        for phase, digest in launch_rows:
            pg.execute(
                "INSERT INTO jeff_proof.launch_events VALUES (%s,%s,%s)",
                (intent.attempt_id, phase, digest),
            )
        counts["launches"] += 1
        _import_result(db, pg, snapshot, counts)
    for approval, job, project, identity, ack in db.execute(
        "SELECT a.approval_id,w.job_id,a.project_id,w.notification_id,w.acknowledged "
        "FROM execution_job_watches w JOIN execution_authorities a USING (job_id)"
    ):
        if UUID(identity) != notification_id_for(UUID(project), UUID(job)):
            raise StorageFault("storage_unrecognized")
        pg.execute(
            "INSERT INTO jeff_proof.watches VALUES (%s,%s,%s,%s)",
            (approval, project, identity, bool(ack)),
        )
        counts["watches"] += 1
    for approval, raw, digest in db.execute(
        "SELECT a.approval_id,l.record_json,l.record_digest FROM execution_lifecycles l "
        "JOIN execution_authorities a USING (proposal_id)"
    ):
        stop = ExecutionStopRecord.model_validate_json(raw)
        # Old DB12 records may omit newly optional scope fields. Check the original stored hash.
        from jeff.domain.execution import fingerprint

        if fingerprint(json.loads(raw)) != digest or str(stop.scope.approval_id) != approval:
            raise StorageFault("storage_unrecognized")
        pg.execute("INSERT INTO jeff_proof.stops VALUES (%s,%s::jsonb)", (approval, raw))
        counts["stops"] += 1
    _import_stop_outputs(db, pg)
    pg.execute(
        "SELECT setval(pg_get_serial_sequence('jeff_proof.results','sequence'), "
        "COALESCE((SELECT max(sequence) FROM jeff_proof.results),1), "
        "EXISTS(SELECT 1 FROM jeff_proof.results))"
    )
    return counts


def _import_result(
    db: sqlite3.Connection, pg: Connection, launch: DockerLaunchSnapshot, counts: dict[str, int]
) -> None:
    row = db.execute(
        "SELECT sequence,disposition,recorded_at_ms,metadata_json,completion_digest,"
        "stdout,stderr,retirement_reason FROM execution_results WHERE attempt_id=?",
        (str(launch.intent.attempt_id),),
    ).fetchone()
    if row is None:
        return
    sequence, disposition, stamp, metadata_json, digest, stdout, stderr, reason = row
    metadata = json.loads(metadata_json)
    output = (
        None
        if stdout is None
        else DockerOutputSnapshot.model_validate(
            dict(metadata["output_facts"], stdout=stdout, stderr=stderr)
        )
    )
    if launch.container_id is None or launch.created_at is None:
        raise StorageFault("storage_unrecognized")
    result = DockerExecutionResult.model_validate(
        metadata
        | dict(
            owned=DockerOwnedContainer(
                intent=launch.intent, container_id=launch.container_id, created_at=launch.created_at
            ),
            sequence=sequence,
            disposition=disposition,
            recorded_at_ms=stamp,
            completion_digest=digest,
            payload_state="retired" if output is None else "retained",
            retirement_reason=reason,
            output=output,
        )
    )
    approval = launch.intent.record.approval_id
    report = db.execute(
        "SELECT status,report_json,report_digest FROM execution_pytest_reports WHERE attempt_id=?",
        (str(launch.intent.attempt_id),),
    ).fetchone()
    report_text, report_digest = None, None
    if report is not None:
        status, raw, original_digest = report
        if output is None or (
            raw is not None and sha256((digest + raw).encode()).hexdigest() != original_digest
        ):
            raise StorageFault("storage_unrecognized")
        report_text = PytestResult.model_validate(
            dict(
                status=status, report=None if raw is None else PytestReport.model_validate_json(raw)
            )
        ).model_dump_json()
        report_digest = sha256((digest + report_text).encode()).hexdigest()
        counts["reports"] += 1
    pg.execute(
        "INSERT INTO jeff_proof.results (approval_id,sequence,facts) OVERRIDING SYSTEM VALUE "
        "VALUES (%s,%s,%s::jsonb)",
        (
            approval,
            sequence,
            result.model_dump_json(exclude={"sequence", "output", "pytest_result"}),
        ),
    )
    pg.execute(
        "INSERT INTO jeff_proof.output VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s)",
        (
            approval,
            stamp,
            disposition == "quarantined",
            None if output is None else output.model_dump_json(),
            reason,
            report_text,
            report_digest,
        ),
    )
    counts["results"] += 1


def _import_stop_outputs(db: sqlite3.Connection, pg: Connection) -> None:
    for approval, disposition, stamp, raw, digest, stdout, stderr, reason in db.execute(
        "SELECT a.approval_id,p.disposition,p.recorded_at_ms,p.output_facts_json,p.output_digest,"
        "p.stdout,p.stderr,p.retirement_reason FROM execution_stop_payloads p "
        "JOIN execution_authorities a USING (proposal_id)"
    ):
        text = (
            None
            if stdout is None
            else DockerOutputSnapshot.model_validate(
                json.loads(raw) | dict(stdout=stdout, stderr=stderr)
            ).model_dump_json()
        )
        if text is not None and sha256(text.encode()).hexdigest() != digest:
            raise StorageFault("storage_unrecognized")
        pg.execute(
            "INSERT INTO jeff_proof.output VALUES (%s,%s,%s,%s::jsonb,%s,NULL,NULL)",
            (approval, stamp, disposition == "quarantined", text, reason),
        )


def _archive_legacy(db: sqlite3.Connection, pg: Connection) -> dict[str, int]:
    # These records stay available for later parity work, not active launch/review authority.
    pg.execute(
        "CREATE TABLE jeff_proof.db12_archive (source_table text, ordinal bigint, "
        "row_data jsonb NOT NULL, PRIMARY KEY (source_table,ordinal))"
    )
    counts = {}
    for table in (
        "schema_migrations",
        "activity_requests",
        "execution_reviews",
        "execution_operations",
    ):
        rows = db.execute(f"SELECT * FROM {table}")
        assert rows.description is not None
        names = tuple(item[0] for item in rows.description)
        count = 0
        for count, row in enumerate(rows, 1):
            pg.execute(
                "INSERT INTO jeff_proof.db12_archive VALUES (%s,%s,%s::jsonb)",
                (table, count, json.dumps(dict(zip(names, row, strict=True)))),
            )
        counts[f"archived_{table}"] = count
    return counts
