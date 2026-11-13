"""Review CLI uses its explicit port; help and syntax do not construct clients."""

from typing import Never

from typer.testing import CliRunner

from jeff.application.execution import ExecutionCall, ExecutionReply, execution_failure
from jeff.interfaces.cli import create_app


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
