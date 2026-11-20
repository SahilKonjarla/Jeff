"""Real authenticated daemon/CLI review flow; never launch the proposed executable."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from jeff.application.execution import ExecutionProfile
from jeff.domain.projects import Project
from jeff.infrastructure.project_files import FileProjectStore

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


def test_real_cli_proposes_reviews_and_restarts_without_launching_user_work(
    git_repo: Path, tmp_path: Path, cli_environment: dict[str, str]
) -> None:
    marker = tmp_path / "must-remain-absent"
    executable = tmp_path / "must-not-run"
    executable.write_text(f"#!/bin/sh\nprintf unexpected > '{marker}'\n")
    executable.chmod(0o700)
    project = Project(uuid4(), git_repo)
    FileProjectStore().initialize(project)
    profile = ExecutionProfile(
        name="python",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="declared-no-engine",
        endpoint="unix:///private/tmp/nonexistent-declared-engine.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable=str(executable),
    )
    data = Path(cli_environment["JEFF_DATA_DIR"])
    data.mkdir(mode=0o700)
    settings = data / "settings.yaml"
    settings.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "history": {"retention_days": 30},
                "execution": {"profiles": [profile.model_dump(mode="json")]},
            }
        )
    )
    settings.chmod(0o600)
    env = {"PATH": os.defpath, "NO_COLOR": "1", "TERM": "dumb", **cli_environment}

    def command(*args: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, "-m", "jeff", *args],
            cwd=git_repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        return cast(dict[str, Any], json.loads(result.stdout))

    try:
        assert command("start", "--json")["data"]["application_ready"] is True
        command("init", "--json")
        proposed = command("run", "--profile", "python", "--json", "--", str(executable))
        record = proposed["data"]["items"][0]
        assert record["state"] == "pending" and record["execution_available"] is False
        assert not marker.exists()
        approved = command("approvals", "approve", record["approval_id"], "--json")
        assert approved["data"]["items"][0]["state"] == "approved"
        assert approved["data"]["items"][0]["job_state"] == "blocked"
        assert not marker.exists()
        shown = command("jobs", "show", record["job_id"], "--json")
        assert shown["data"]["items"][0]["launch_authority_issued"] is False
        assert len(command("approvals", "list", "--json")["data"]["items"]) == 1
        command("stop", "--json")
        command("start", "--json")
        retained = command("jobs", "show", record["job_id"], "--json")
        assert retained["data"]["items"][0]["state"] == "invalidated"
        assert not marker.exists()
    finally:
        command("stop", "--json")
