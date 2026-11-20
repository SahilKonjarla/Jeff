"""Fresh list CLI is lazy, scoped, bounded and separate from legacy review commands."""

from pathlib import Path
from typing import Never
from uuid import uuid4

from typer.testing import CliRunner

from jeff.application.execution import FreshExecutionCall, FreshExecutionPage, FreshExecutionReply
from jeff.interfaces.cli import create_app


def test_fresh_list_cli_is_lazy_and_preserves_pagination() -> None:
    calls: list[FreshExecutionCall] = []

    def forbidden() -> Never:
        raise AssertionError("Unrelated factory invoked")

    class Client:
        def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply:
            calls.append(call)
            return FreshExecutionReply(
                ok=True,
                request_id=call.request_id,
                operation=call.operation,
                identity=uuid4(),
                page=FreshExecutionPage(project_id=uuid4(), canonical_root=call.directory),
            )

    app = create_app(forbidden, forbidden, fresh_execution=lambda: Client())
    runner = CliRunner()
    for name in ("approvals", "jobs"):
        assert runner.invoke(app, ["execution", name, "list", "--help"]).exit_code == 0
    assert not calls
    cursor = f"1000:{uuid4()}"
    for name in ("approvals", "jobs"):
        result = runner.invoke(
            app, ["execution", name, "list", "--limit", "2", "--after", cursor, "--json"]
        )
        assert result.exit_code == 0, result.output
        reply = FreshExecutionReply.model_validate_json(result.stdout)
        assert reply.page is not None and reply.page.items == ()
        assert calls[-1].operation == name + ".list"
        assert calls[-1].after == cursor and calls[-1].limit == 2
        assert calls[-1].directory == Path.cwd()
    assert runner.invoke(app, ["execution", "jobs", "list", "--limit", "33"]).exit_code == 2
    assert len(calls) == 2
    bad = runner.invoke(app, ["execution", "jobs", "list", "--after", "invalid"])
    assert bad.exit_code == 1 and len(calls) == 2
