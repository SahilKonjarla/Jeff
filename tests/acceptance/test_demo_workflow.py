"""Documented generator CLI followed by installed Jeff against both demo roots."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

from jeff.application.projects import Success
from jeff.application.questions import BranchAnswer, ChangesAnswer, QuestionSuccess

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/create_demo_projects.py"


def invoke(directory: Path, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=directory,
        env={
            "PATH": os.defpath,
            "NO_COLOR": "1",
            "TERM": "dumb",
            "GIT_DIR": "/nonexistent/SYNTHETIC_PRIVATE_VALUE",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_generator_cli_and_installed_jeff_for_both_projects(tmp_path: Path) -> None:
    output = tmp_path / "demos"
    generated = invoke(tmp_path, [sys.executable, str(SCRIPT), "--output-dir", str(output)])
    assert generated.returncode == 0, generated.stderr
    assert generated.stdout.count("Created:") == 2
    jeff = str(Path(sysconfig.get_path("scripts")) / "jeff")
    identities: list[str] = []
    configs: list[bytes] = []
    for name in ("sandbox-project", "second-project"):
        root = output / name
        initialized = invoke(root, [jeff, "init", "--json"])
        assert initialized.returncode == 0, initialized.stderr
        identity = Success.model_validate_json(initialized.stdout)
        identities.append(identity.data.project_id)
        configs.append((root / "jeff.local.yaml").read_bytes())
        for operation in ("config", "status", "init"):
            checked = invoke(root, [jeff, operation, "--json"])
            assert checked.returncode == 0, checked.stderr
            assert (
                Success.model_validate_json(checked.stdout).data.project_id
                == identity.data.project_id
            )
        branch_result = invoke(root, [jeff, "ask", "what branch am I on?", "--json"])
        assert branch_result.returncode == 0, branch_result.stderr
        branch = QuestionSuccess.model_validate_json(branch_result.stdout)
        assert isinstance(branch.data, BranchAnswer) and branch.data.branch.name == "main"
        assert branch.data.branch.commit is not None and not branch.data.branch.unborn
        changed_result = invoke(root, [jeff, "ask", "what changed?", "--json"])
        assert changed_result.returncode == 0, changed_result.stderr
        changed = QuestionSuccess.model_validate_json(changed_result.stdout)
        assert isinstance(changed.data, ChangesAnswer)
        assert [change.path for change in changed.data.changes] == ["demo-notes.txt"]
        assert changed.data.project_id == identity.data.project_id
    assert len(set(identities)) == 2
    repeated = invoke(tmp_path, [sys.executable, str(SCRIPT), "--output-dir", str(output)])
    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout.count("Preserved:") == 2
    for name, config in zip(("sandbox-project", "second-project"), configs, strict=True):
        assert (output / name / "jeff.local.yaml").read_bytes() == config


def test_script_help_has_no_side_effects_and_reports_refusal(tmp_path: Path) -> None:
    help_result = invoke(tmp_path, [sys.executable, str(SCRIPT), "--help"])
    assert help_result.returncode == 0 and "--output-dir" in help_result.stdout
    assert not list(tmp_path.iterdir())
    target = tmp_path / "sandbox-project"
    target.mkdir()
    (target / "user.txt").write_text("SYNTHETIC_PRIVATE_VALUE")
    refused = invoke(tmp_path, [sys.executable, str(SCRIPT), "--output-dir", str(tmp_path)])
    assert refused.returncode == 1 and "refused" in refused.stderr
    assert "SYNTHETIC_PRIVATE_VALUE" not in refused.stdout + refused.stderr
    assert not (tmp_path / "second-project").exists()
