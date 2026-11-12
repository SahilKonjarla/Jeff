"""Public debug/history tracer through the authenticated runtime and lazy CLI."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, NoReturn

import pytest
from typer.testing import CliRunner

from jeff.application.projects import ProjectService
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.integrations.git import GitProjectDiscovery
from jeff.interfaces.cli import create_app

runner = CliRunner()


def forbidden() -> NoReturn:
    raise AssertionError("Help/syntax must not select runtime or instantiate clients")


@pytest.mark.parametrize(
    "args",
    [
        ["debug", "--help"],
        ["debug", "last", "--help"],
        ["debug", "last", "--request-id", "bad"],
        ["history", "--help"],
        ["history", "--limit", "0"],
        ["history", "--limit", "101"],
        ["history", "--limit", "bad"],
    ],
)
def test_debug_lazy_help_and_invalid_uuid(args: list[str]) -> None:
    result = runner.invoke(create_app(forbidden, forbidden), args)
    assert result.exit_code == (0 if "--help" in args else 2)


@pytest.mark.usefixtures("owned_runtime_launches")
def test_debug_real_daemon_latest_exact_and_read_only(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(git_repo)
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    control = RuntimeController(layout)
    app = create_app(lambda: RuntimeProjectClient(layout), lambda: control)
    try:
        stopped = runner.invoke(app, ["debug", "last", "--json"])
        assert stopped.exit_code == 1
        assert json.loads(stopped.stdout)["error"]["code"] == "daemon_not_running"
        # Fixture registration deliberately has never been indexed by a daemon.
        registered = ProjectService(GitProjectDiscovery(), FileProjectStore()).execute(
            "init", git_repo
        )
        assert registered.ok
        assert isinstance(control.start(), ControlSuccess)
        empty_before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
        empty = runner.invoke(app, ["history", "--json"])
        assert empty.exit_code == 0 and json.loads(empty.stdout)["data"]["items"] == []
        missing = runner.invoke(app, ["debug", "last", "--json"])
        assert missing.exit_code == 1
        assert json.loads(missing.stdout)["error"]["code"] == "no_history"
        assert empty_before == (layout.database.read_bytes(), layout.database.stat().st_mtime_ns)
        assert isinstance(control.status(), ControlSuccess)
        assert runner.invoke(app, ["init", "--json"]).exit_code == 0
        assert runner.invoke(app, ["ask", "what branch am I on?", "--json"]).exit_code == 0
        other = tmp_path / "second"
        other.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(other)], check=True, timeout=5)
        with monkeypatch.context() as changed:
            changed.chdir(other)
            assert runner.invoke(app, ["init", "--json"]).exit_code == 0
            assert runner.invoke(app, ["ask", "what branch am I on?", "--json"]).exit_code == 0
        before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
        debug = runner.invoke(app, ["debug", "last", "--json"])
        assert debug.exit_code == 0, debug.exception
        data = json.loads(debug.stdout)["data"]
        assert data["receipt"]["operation"] == "ask.branch"
        assert data["receipt"]["state"] == "succeeded"
        assert data["trace"]["events"][-1]["kind"] == "request.succeeded"
        assert runner.invoke(app, ["config", "--json"]).exit_code == 0
        again = runner.invoke(
            app, ["debug", "last", "--request-id", data["receipt"]["request_id"], "--json"]
        )
        assert again.exit_code == 0
        assert json.loads(again.stdout)["data"] == data
        assert before == (layout.database.read_bytes(), layout.database.stat().st_mtime_ns)
        history = runner.invoke(app, ["history", "--limit", "1", "--json"])
        assert history.exit_code == 0, history.exception
        page = json.loads(history.stdout)["data"]
        assert page["items"][0]["request_id"] == data["receipt"]["request_id"]
        assert page["next_cursor"]
        rest = runner.invoke(app, ["history", "--cursor", page["next_cursor"], "--json"])
        assert rest.exit_code == 0
        assert json.loads(rest.stdout)["data"]["items"][0]["operation"] == "init"
        invalid = runner.invoke(app, ["history", "--cursor", "SYNTHETIC_PRIVATE", "--json"])
        assert invalid.exit_code == 1
        assert json.loads(invalid.stdout)["error"]["code"] == "activity_invalid_cursor"
        assert "SYNTHETIC_PRIVATE" not in invalid.output
        assert isinstance(control.status(), ControlSuccess)
        assert before == (layout.database.read_bytes(), layout.database.stat().st_mtime_ns)
        assert isinstance(control.stop(), ControlSuccess)
        assert isinstance(control.start(), ControlSuccess)
        restarted = runner.invoke(app, ["debug", "last", "--json"])
        assert restarted.exit_code == 0 and json.loads(restarted.stdout)["data"] == data
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


@pytest.mark.usefixtures("owned_runtime_launches")
def test_owned_v4_v5_hard_refusal_and_preserved_instrumented_history(
    git_repo: Path, tmp_path: Path
) -> None:
    old = os.environ.get("JEFF_TEST_V4_PYTHON")
    if old is None:
        pytest.skip("Requires explicitly verified owned A-v4 installation")
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
    }

    def legacy(operation: str) -> dict[str, Any]:
        code = """
import sys
from pathlib import Path
from jeff.application.runtime import PROTOCOL_REVISION
from jeff.application.dispatch import ProjectCall
from jeff.bootstrap.runtime import create_runtime_controller, create_project_client
assert PROTOCOL_REVISION == 4
if sys.argv[1] == 'init':
    call = ProjectCall(operation='init',directory=Path(sys.argv[2]))
    result = create_project_client().execute(call)
else:
    result = getattr(create_runtime_controller(), sys.argv[1])()
print(result.model_dump_json())
"""
        child = subprocess.run(
            [old, "-I", "-B", "-c", code, operation, str(git_repo)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            timeout=45,
        )
        assert child.returncode == 0, "Owned legacy helper failed; raw output suppressed"
        result: dict[str, Any] = json.loads(child.stdout)
        return result

    try:
        assert legacy("start")["ok"]
        initialized = legacy("init")
        assert initialized["result"]["ok"]
        before = layout.credential.stat().st_ino, layout.database.read_bytes()
        refused = RuntimeController(layout).start()
        assert not refused.ok and refused.error.code == "runtime_protocol_incompatible"
        assert before == (layout.credential.stat().st_ino, layout.database.read_bytes())
    finally:
        assert legacy("stop")["ok"]
    control = RuntimeController(layout)
    try:
        assert isinstance(control.start(), ControlSuccess)
        from jeff.application.dispatch import DebugQuery, HistoryQuery

        client = RuntimeProjectClient(layout)
        debug = client.inspect_evidence(DebugQuery(directory=git_repo))
        assert debug.error is None and debug.receipt is not None and debug.trace is not None
        assert str(debug.receipt.request_id) == initialized["request_id"]
        assert not debug.trace.legacy_metadata_only
        page = client.inspect_evidence(HistoryQuery(directory=git_repo))
        assert (
            page.history is not None
            and page.history.items[0].request_id == debug.receipt.request_id
        )
        old_status = legacy("status")
        assert not old_status["ok"]
        assert old_status["error"]["code"] == "runtime_protocol_incompatible"
        assert isinstance(control.status(), ControlSuccess)
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
