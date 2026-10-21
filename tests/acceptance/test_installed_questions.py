"""Real installed ask commands, outside the source repository."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from jeff.application.projects import Failure, Success
from jeff.application.questions import BranchAnswer, ChangesAnswer, QuestionSuccess


def run(root: Path, entry: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    prefix = (
        [str(Path(sysconfig.get_path("scripts")) / "jeff")]
        if entry == "console"
        else [sys.executable, "-m", "jeff"]
    )
    return subprocess.run(
        [*prefix, *arguments],
        cwd=root,
        env={
            "PATH": os.defpath,
            "NO_COLOR": "1",
            "TERM": "dumb",
            "GIT_DIR": "/invalid/FAKE_TEST_SENTINEL",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("entry", ["console", "module"])
def test_installed_questions_are_project_bound_and_read_only(git_repo: Path, entry: str) -> None:
    initialized = run(git_repo, entry, "init", "--json")
    assert initialized.returncode == 0, initialized.stderr
    identity = Success.model_validate_json(initialized.stdout)
    config = (git_repo / "jeff.local.yaml").read_bytes()
    head = (git_repo / ".git/HEAD").read_bytes()
    assert not (git_repo / ".git/index").exists()
    nested = git_repo / "src"
    nested.mkdir()
    (nested / "line\n\x1b[31m.txt").write_text("visible synthetic fixture")
    branch_result = run(nested, entry, "ask", "what branch am I on?", "--json")
    assert branch_result.returncode == 0, branch_result.stderr
    branch = QuestionSuccess.model_validate_json(branch_result.stdout)
    assert isinstance(branch.data, BranchAnswer)
    assert branch.data.branch.name == "main" and branch.data.branch.unborn
    assert branch.data.project_id == identity.data.project_id and branch.data.root == str(git_repo)
    assert branch.data.observed_at.utcoffset() is not None
    changes_result = run(nested, entry, "ask", "what changed?", "--json")
    assert changes_result.returncode == 0, changes_result.stderr
    changes = QuestionSuccess.model_validate_json(changes_result.stdout)
    assert isinstance(changes.data, ChangesAnswer) and not changes.data.complete
    assert r"src/line\n\u001b[31m.txt" in [change.path for change in changes.data.changes]
    human = run(nested, entry, "ask", "what changed?")
    assert human.returncode == 0 and "\x1b" not in human.stdout
    assert r"src/line\n\u001b[31m.txt" in human.stdout and "not full status" in human.stdout
    assert (git_repo / "jeff.local.yaml").read_bytes() == config
    assert (git_repo / ".git/HEAD").read_bytes() == head
    assert not (git_repo / ".git/index").exists()
    assert not (git_repo / ".git/index.lock").exists()


def test_installed_unknown_and_unregistered_errors(git_repo: Path) -> None:
    unknown = run(git_repo, "module", "ask", "FAKE_TEST_SENTINEL", "--json")
    assert unknown.returncode == 1
    assert Failure.model_validate_json(unknown.stdout).error.code == "unsupported_intent"
    assert "FAKE_TEST_SENTINEL" not in unknown.stdout + unknown.stderr
    missing = run(git_repo, "console", "ask", "what changed?", "--json")
    assert missing.returncode == 1
    assert Failure.model_validate_json(missing.stdout).error.code == "project_not_initialized"
    assert not (git_repo / "jeff.local.yaml").exists()
