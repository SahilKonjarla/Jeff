"""Operator-facing policy and read-only recovery guidance; injected clients only."""

from typing import Never
from uuid import UUID

import pytest
from test_guard_execution import guarded as guarded
from typer.testing import CliRunner

from jeff.application.execution import (
    AuthorityRecord,
    FreshExecutionCall,
    FreshExecutionReply,
    FreshExecutionView,
    GuardedExecutionManifest,
    fresh_execution_failure,
)
from jeff.domain.execution import AuthorityState
from jeff.interfaces.cli import create_app


def forbidden() -> Never:
    raise AssertionError("No unrelated client, native process or recovery mutation")


def test_proposal_warns_about_public_input_responsibility_without_polluting_json() -> None:
    calls: list[FreshExecutionCall] = []

    class Client:
        def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply:
            calls.append(call)
            return FreshExecutionReply(
                ok=False,
                request_id=call.request_id,
                operation=call.operation,
                error=fresh_execution_failure("execution_unavailable"),
            )

    app = create_app(forbidden, forbidden, fresh_execution=lambda: Client())
    result = CliRunner().invoke(
        app,
        [
            "execution",
            "propose",
            "--profile",
            "python",
            "--input",
            "source.py",
            "--json",
            "--",
            "python",
            "-V",
        ],
    )
    assert result.exit_code == 1 and len(calls) == 1
    assert "non-secret public" in result.stderr
    assert "not secret certification" in result.stderr
    assert "persist" in result.stderr
    parsed = FreshExecutionReply.model_validate_json(result.stdout)
    assert parsed.error is not None and parsed.error.error.code == "execution_unavailable"
    assert calls[0].paths == ("source.py",)


@pytest.mark.parametrize("json_output", [False, True])
def test_unknown_job_offers_read_only_original_id_guidance_without_repair_calls(
    guarded: GuardedExecutionManifest,
    json_output: bool,
) -> None:
    record = AuthorityRecord(
        proposal_id=UUID(int=10),
        approval_id=UUID(int=11),
        job_id=UUID(int=12),
        project_id=guarded.profile.project_id,
        canonical_root=guarded.profile.canonical_root,
        owner_instance_id=UUID(int=13),
        manifest=guarded,
        manifest_digest=guarded.fingerprint,
        state=AuthorityState.CONSUMED,
        created_at_ms=1,
        expires_at_ms=600001,
        claim_id=UUID(int=14),
        claim_state="unknown",
        claimed_at_ms=2,
    )
    calls: list[FreshExecutionCall] = []

    class Client:
        def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply:
            calls.append(call)
            return FreshExecutionReply(
                ok=True,
                request_id=call.request_id,
                operation=call.operation,
                identity=UUID(int=15),
                view=FreshExecutionView.from_record(record),
                completion_state="claim_only",
                execution_phase="unconfirmed",
            )

    app = create_app(forbidden, forbidden, fresh_execution=lambda: Client())
    args = ["execution", "show", str(record.approval_id)] + (["--json"] if json_output else [])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0 and len(calls) == 1 and calls[0].operation == "show"
    if json_output:
        parsed = FreshExecutionReply.model_validate_json(result.stdout)
        assert parsed.view is not None and parsed.view.claim_state == "unknown"
        assert (
            not {"claim_id", "owner_instance_id", "container_id", "nonce"}
            & parsed.view.model_dump().keys()
        )
    else:
        assert f"jeff execution show {record.approval_id} --json" in result.stdout
        assert f"jeff execution logs {record.approval_id} --json" in result.stdout
        assert "Do not retry, reset or adopt" in result.stdout
        assert "Missing logs" in result.stdout


def test_unknown_cancellation_guidance_does_not_turn_intent_into_recovery() -> None:
    from jeff.application.execution import (
        ExecutionCancelCall,
        ExecutionCancelReply,
        ExecutionStopView,
    )

    calls: list[ExecutionCancelCall] = []

    class Client:
        def cancel(self, call: ExecutionCancelCall) -> ExecutionCancelReply:
            calls.append(call)
            return ExecutionCancelReply(
                request_id=call.request_id,
                approval_id=call.approval_id,
                reviewed_manifest_digest=call.reviewed_manifest_digest,
                identity=UUID(int=1),
                ok=True,
                stop=ExecutionStopView(
                    request_id=call.request_id,
                    reason="cancel",
                    requested_at_ms=1,
                    clock_confirmed=True,
                    outcome="unknown",
                    evidence="uncertain",
                    cleanup_verified=False,
                ),
            )

    app = create_app(forbidden, forbidden, cancellation=lambda: Client())
    approval = UUID(int=11)
    result = CliRunner().invoke(app, ["execution", "cancel", str(approval), "--digest", "a" * 64])
    assert result.exit_code == 0 and len(calls) == 1
    assert "Termination: unknown; cleanup: False" in result.stdout
    assert f"jeff execution show {approval} --json" in result.stdout
    assert "Do not retry, reset or adopt" in result.stdout


def test_unknown_listing_is_discovery_not_cleanup_authority(
    guarded: GuardedExecutionManifest,
) -> None:
    from jeff.application.execution import FreshExecutionPage

    view = FreshExecutionView(
        authority_kind=guarded.authority_kind,
        proposal_id=UUID(int=10),
        approval_id=UUID(int=11),
        job_id=UUID(int=12),
        project_id=guarded.profile.project_id,
        canonical_root=guarded.profile.canonical_root,
        manifest=guarded,
        manifest_digest=guarded.fingerprint,
        state=AuthorityState.CONSUMED,
        created_at_ms=1,
        expires_at_ms=600001,
        claim_state="unknown",
    )
    calls: list[FreshExecutionCall] = []

    class Client:
        def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply:
            calls.append(call)
            return FreshExecutionReply(
                ok=True,
                request_id=call.request_id,
                operation=call.operation,
                identity=UUID(int=1),
                page=FreshExecutionPage(
                    project_id=view.project_id, canonical_root=view.canonical_root, items=(view,)
                ),
            )

    app = create_app(forbidden, forbidden, fresh_execution=lambda: Client())
    result = CliRunner().invoke(app, ["execution", "jobs", "list"])
    assert result.exit_code == 0 and len(calls) == 1 and calls[0].operation == "jobs.list"
    assert "Discovery only" in result.stdout and "Do not retry, reset or adopt" in result.stdout
    assert "jeff execution show APPROVAL_ID --json" in result.stdout


@pytest.mark.parametrize("code", ["execution_unconfirmed", "request_outcome_unknown"])
def test_uncertain_failure_messages_explain_preservation_and_inspection(code: str) -> None:
    message = fresh_execution_failure(code).error.message
    assert "execution show" in message and "execution logs" in message
    assert "original" in message and "reset" in message and "adopt" in message
