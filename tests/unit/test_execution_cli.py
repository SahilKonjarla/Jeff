"""Review CLI uses its explicit port; help and syntax do not construct clients."""

from typing import Never

from typer.testing import CliRunner

from jeff.application.execution import ExecutionCall, ExecutionReply, execution_failure
from jeff.interfaces.cli import create_app


def test_fresh_cli_is_lazy_distinct_and_requires_reviewed_digest() -> None:
    from uuid import uuid4

    from jeff.application.execution import (
        FreshExecutionCall,
        FreshExecutionReply,
        fresh_execution_failure,
    )

    calls: list[FreshExecutionCall] = []

    def forbidden() -> Never:
        raise AssertionError("unrelated factory invoked")

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
    runner = CliRunner()
    for args in (
        ["execution", "--help"],
        ["execution", "propose", "--help"],
        ["execution", "start", "--help"],
    ):
        assert runner.invoke(app, args).exit_code == 0
    assert not calls
    identity = str(uuid4())
    object_id = str(uuid4())
    proposed = runner.invoke(
        app,
        [
            "execution",
            "propose",
            "--profile",
            "python",
            "--input",
            "source.py",
            "--request-id",
            identity,
            "--json",
            "--",
            "python",
            "-c",
            "print(42)",
        ],
    )
    assert proposed.exit_code == 1 and "execution_unavailable" in proposed.stdout
    assert len(calls) == 1 and str(calls[0].request_id) == identity
    assert calls[0].argv == ("python", "-c", "print(42)") and calls[0].paths == ("source.py",)
    missing = runner.invoke(app, ["execution", "start", object_id])
    assert missing.exit_code == 2 and len(calls) == 1
    for operation in ("approve", "deny", "start"):
        result = runner.invoke(
            app, ["execution", operation, object_id, "--digest", "0" * 64, "--json"]
        )
        assert result.exit_code == 1 and calls[-1].reviewed_manifest_digest == "0" * 64
    for operation in ("show", "logs"):
        runner.invoke(app, ["execution", operation, object_id, "--json"])
        assert calls[-1].operation == operation and calls[-1].reviewed_manifest_digest is None


def test_run_passes_literal_argv_to_review_port_without_invoking_project_factories() -> None:
    calls: list[ExecutionCall] = []

    def forbidden() -> Never:
        raise AssertionError("unrelated factory invoked")

    class Client:
        def review(self, call: ExecutionCall) -> ExecutionReply:
            calls.append(call)
            return ExecutionReply(
                request_id=call.request_id, result=execution_failure("execution_profile_missing")
            )

    app = create_app(forbidden, forbidden, execution=lambda: Client())
    runner = CliRunner()
    assert runner.invoke(app, ["run", "--help"]).exit_code == 0
    assert not calls
    result = runner.invoke(app, ["run", "--json", "--", "python", "-c", "print(42)"])
    assert result.exit_code == 1
    assert "execution_profile_missing" in result.stdout
    assert len(calls) == 1
    assert calls[0].argv == ("python", "-c", "print(42)")
