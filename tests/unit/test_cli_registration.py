"""CLI package extraction preserves lazy registration and operation bindings."""

from typing import Never
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from jeff.application.execution import (
    ExecutionCall,
    ExecutionReply,
    execution_failure,
)
from jeff.interfaces.cli import create_app

HELP_PATHS = [
    [],
    ["run"],
    ["doctor"],
    ["start"],
    ["stop"],
    ["init"],
    ["config"],
    ["status"],
    ["ask"],
    ["history"],
    ["debug"],
    ["debug", "last"],
    ["approvals"],
    ["approvals", "list"],
    ["approvals", "show"],
    ["approvals", "approve"],
    ["approvals", "deny"],
    ["jobs"],
    ["jobs", "list"],
    ["jobs", "show"],
    ["execution"],
    ["execution", "cancel"],
    ["execution", "propose"],
    ["execution", "approve"],
    ["execution", "deny"],
    ["execution", "start"],
    ["execution", "show"],
    ["execution", "logs"],
]


def forbidden() -> Never:
    raise AssertionError("Help must not construct any client or select runtime state")


@pytest.mark.parametrize("path", HELP_PATHS)
def test_all_command_help_remains_lazy(path: list[str]) -> None:
    app = create_app(forbidden, forbidden, forbidden, forbidden, forbidden, forbidden)
    result = CliRunner().invoke(app, [*path, "--help"])
    assert result.exit_code == 0, result.exception


@pytest.mark.parametrize(
    ("group", "command", "expected"),
    [
        ("approvals", "list", "approval.list"),
        ("approvals", "show", "approval.show"),
        ("approvals", "approve", "approve"),
        ("approvals", "deny", "deny"),
        ("jobs", "list", "job.list"),
        ("jobs", "show", "job.show"),
    ],
)
def test_review_groups_keep_distinct_operation_bindings(
    group: str, command: str, expected: str
) -> None:
    calls: list[ExecutionCall] = []

    class Client:
        def review(self, call: ExecutionCall) -> ExecutionReply:
            calls.append(call)
            return ExecutionReply(
                request_id=call.request_id, result=execution_failure("execution_invalid")
            )

    app = create_app(forbidden, forbidden, execution=lambda: Client())
    object_id, request_id = uuid4(), uuid4()
    arguments = [group, command]
    if command != "list":
        arguments.append(str(object_id))
    if command in {"approve", "deny"}:
        arguments.extend(["--request-id", str(request_id)])
    result = CliRunner().invoke(app, [*arguments, "--json"])
    assert result.exit_code == 1, result.exception
    assert len(calls) == 1 and calls[0].operation == expected
    if command != "list":
        assert calls[0].object_id == object_id
    if command in {"approve", "deny"}:
        assert calls[0].request_id == request_id
    assert result.stderr == f"Request: {calls[0].request_id}\n"
    assert "execution_invalid" in result.stdout
