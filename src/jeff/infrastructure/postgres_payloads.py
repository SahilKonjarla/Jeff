"""Proof-only retained output and pytest reports, sharing one retirement slot."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.execution import DockerBackendFault, DockerOutputSnapshot
from jeff.application.execution.pytest_report import (
    PytestResult,
    pytest_run_token,
    read_pytest_report,
)
from jeff.application.storage import StorageFault

if TYPE_CHECKING:
    from jeff.application.execution import DockerCompletion, DockerExecutionResult

    from .postgres_storage import Connection

OUTPUT_DDL = (
    "CREATE TABLE jeff_proof.output (approval_id uuid PRIMARY KEY "
    "REFERENCES jeff_proof.authorities(approval_id), recorded_at_ms bigint NOT NULL, "
    "quarantined boolean NOT NULL, payload jsonb, retirement_reason text "
    "CHECK(retirement_reason IN ('age','capacity')), "
    "report_text text CHECK (octet_length(report_text)<=65664), "
    "report_digest text CHECK (length(report_digest)=64), "
    "CHECK ((payload IS NULL)=(retirement_reason IS NOT NULL)), "
    "CHECK ((report_text IS NULL)=(report_digest IS NULL)), "
    "CHECK (payload IS NOT NULL OR report_text IS NULL))",
    "CREATE UNIQUE INDEX proof_one_quarantined_capture ON jeff_proof.output(quarantined) "
    "WHERE quarantined AND payload IS NOT NULL",
)


def encode_report(completion: DockerCompletion) -> tuple[str | None, str | None]:
    result = completion.pytest_result
    if result is None:
        return None, None
    manifest = completion.owned.intent.record.manifest
    token = pytest_run_token(manifest.argv)
    if token is None or not completion.confirmed:
        raise DockerBackendFault("docker_output_failed")
    if result.report is not None:
        assert completion.outcome.exit_code is not None
        _, checked = read_pytest_report(
            result.report.model_dump_json().encode(),
            run_token=token,
            exit_status=completion.outcome.exit_code,
            approved_paths={item.path for item in manifest.inputs.files},
        )
        if checked != result.report:
            raise DockerBackendFault("docker_output_failed")
    text = result.model_dump_json()
    digest = sha256((completion.fingerprint + text).encode()).hexdigest()
    return text, digest


def decode_report(
    result: DockerExecutionResult, text: object, digest: object
) -> PytestResult | None:
    if text is None and digest is None:
        return None
    if (
        not isinstance(text, str)
        or result.payload_state != "retained"
        or sha256((result.completion_digest + text).encode()).hexdigest() != digest
    ):
        raise StorageFault("storage_unrecognized")
    return PytestResult.model_validate_json(text)


def archive(
    connection: Connection,
    approval: UUID,
    output: DockerOutputSnapshot,
    now: int,
    *,
    quarantined: bool,
    report: tuple[str | None, str | None] = (None, None),
) -> None:
    if (
        quarantined
        and connection.execute(
            "SELECT 1 FROM jeff_proof.output WHERE quarantined AND payload IS NOT NULL LIMIT 1"
        ).fetchone()
        is not None
    ):
        raise DockerBackendFault("docker_busy")
    connection.execute(
        "INSERT INTO jeff_proof.output VALUES (%s,%s,%s,%s::jsonb,NULL,%s,%s)",
        (approval, now, quarantined, output.model_dump_json(), *report),
    )
    connection.execute(
        "UPDATE jeff_proof.output SET payload=NULL,report_text=NULL,report_digest=NULL, "
        "retirement_reason='capacity' WHERE approval_id IN (SELECT approval_id "
        "FROM jeff_proof.output WHERE NOT quarantined AND payload IS NOT NULL "
        "ORDER BY recorded_at_ms DESC, approval_id DESC OFFSET 16)",
    )


def retire(connection: Connection, cutoff: int) -> int:
    return len(
        connection.execute(
            "UPDATE jeff_proof.output SET payload=NULL,report_text=NULL,report_digest=NULL, "
            "retirement_reason='age' WHERE approval_id IN (SELECT approval_id "
            "FROM jeff_proof.output WHERE payload IS NOT NULL AND recorded_at_ms<%s "
            "ORDER BY recorded_at_ms,approval_id LIMIT 4) RETURNING approval_id",
            (cutoff,),
        ).fetchall()
    )
