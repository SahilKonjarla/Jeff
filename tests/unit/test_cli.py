"""Presentation at lazy application client/control seams; no local action fallback."""

import json
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from jeff.application.dispatch import (
    DispatchReply,
    ProjectCall,
    ReceiptQuery,
    ReceiptReply,
    dispatch_failure,
)
from jeff.application.projects import Failure
from jeff.application.runtime import ControlObservation, ControlSuccess, RuntimeIdentityData
from jeff.domain.runtime import RuntimeState
from jeff.interfaces.cli import create_app

runner = CliRunner()


class Client:
    def __init__(self) -> None:
        self.calls: list[ProjectCall] = []
        self.uncertain = False

    def execute(self, call: ProjectCall) -> DispatchReply:
        self.calls.append(call)
        return DispatchReply(
            request_id=call.request_id,
            identity=RuntimeIdentityData(instance_id=uuid4(), pid=1),
            result=dispatch_failure(
                "request_outcome_unknown" if self.uncertain else "unsupported_intent"
            ),
            accepted=None if self.uncertain else False,
        )

    def status(self, directory: Path) -> Failure:
        return dispatch_failure("project_target_mismatch")

    def inspect_receipt(self, query: ReceiptQuery) -> ReceiptReply:
        raise AssertionError("No public receipt-browsing verb in this slice")


class Control:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> ControlSuccess:
        self.calls.append("start")
        return ControlSuccess(data=ControlObservation(state=RuntimeState.STOPPED))

    def stop(self) -> ControlSuccess:
        self.calls.append("stop")
        return ControlSuccess(data=ControlObservation(state=RuntimeState.STOPPED))

    def status(self) -> ControlSuccess:
        raise AssertionError("Project status belongs to the client diagnostic interface")


def forbidden() -> NoReturn:
    raise AssertionError("Help/syntax must not construct clients or select runtime locations")


@pytest.mark.parametrize(
    "arguments", [["--help"], ["start", "--help"], ["ask", "--help"], [], ["run"]]
)
def test_help_and_syntax_are_lazy(
    arguments: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(create_app(forbidden, forbidden), arguments)
    assert result.exit_code == (0 if "--help" in arguments else 2), result.exception
    assert not list(tmp_path.iterdir())
    if arguments == ["--help"]:
        assert all(
            name in result.stdout for name in ("start", "stop", "init", "config", "status", "ask")
        )
        assert "No daemon" not in result.stdout


@pytest.mark.parametrize("operation", ["init", "config", "ask"])
def test_project_commands_use_only_injected_client(
    operation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client, control = Client(), Control()
    arguments = [operation, *(["what changed?"] if operation == "ask" else []), "--json"]
    result = runner.invoke(create_app(lambda: client, lambda: control), arguments)
    assert result.exit_code == 1, result.exception
    assert json.loads(result.stdout)["error"]["code"] == "unsupported_intent"
    assert len(client.calls) == 1 and client.calls[0].operation == operation
    assert client.calls[0].directory == tmp_path and not control.calls
    assert not result.stderr and not list(tmp_path.iterdir())


def test_uncertain_correlation_is_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = Client()
    client.uncertain = True
    result = runner.invoke(create_app(lambda: client, Control), ["init", "--json"])
    assert result.exit_code == 1, result.exception
    body = json.loads(result.stdout)
    assert (
        set(body) == {"ok", "data", "error"} and body["error"]["code"] == "request_outcome_unknown"
    )
    assert result.stderr == f"Request: {client.calls[0].request_id}\n"
    assert str(client.calls[0].request_id) not in result.stdout


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_lifecycle_uses_only_control_factory(operation: str) -> None:
    control = Control()
    result = runner.invoke(create_app(forbidden, lambda: control), [operation, "--json"])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["data"]["state"] == "stopped"
    assert control.calls == [operation]
