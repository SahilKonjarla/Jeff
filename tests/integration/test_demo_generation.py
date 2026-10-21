"""Actual isolated demo creation, deterministic baselines, and repeat preservation."""

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from jeff.application.projects import ProjectService
from jeff.infrastructure.project_files import FileProjectStore
from jeff.integrations.git import GitProjectDiscovery
from scripts import create_demo_projects as demo


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git", path=os.defpath)
    assert executable is not None
    return subprocess.run(
        [executable, "-c", "core.hooksPath=/dev/null", "-C", str(root), *arguments],
        env={"PATH": os.defpath, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )


def test_independent_roots_and_parent_git_untouched(git_repo: Path) -> None:
    before_config = (git_repo / ".git/config").read_bytes()
    before_head = (git_repo / ".git/HEAD").read_bytes()
    rules = Path(__file__).resolve().parents[2] / ".gitignore"
    (git_repo / ".gitignore").write_bytes(rules.read_bytes())
    generated = demo.generate(git_repo / "examples")
    assert all(result.created for result in generated)
    assert not (git_repo / ".git/index").exists()
    assert (git_repo / ".git/config").read_bytes() == before_config
    assert (git_repo / ".git/HEAD").read_bytes() == before_head
    for result in generated:
        assert Path(git(result.path, "rev-parse", "--show-toplevel").stdout.strip()) == result.path
        assert git(result.path, "branch", "--show-current").stdout.strip() == "main"
        marker = json.loads((result.path / demo.MARKER).read_text())
        assert marker["baseline_commit"] == git(result.path, "rev-parse", "HEAD").stdout.strip()
        assert git(result.path, "status", "--porcelain").stdout == "?? demo-notes.txt\n"
        assert not git(result.path, "remote").stdout
        assert not (result.path / "jeff.local.yaml").exists()
        ignored = git(git_repo, "check-ignore", "--", str(result.path / "README.md"))
        assert ignored.returncode == 0


def test_same_templates_produce_identical_baseline_commits(tmp_path: Path) -> None:
    first = demo.generate(tmp_path / "one")
    second = demo.generate(tmp_path / "two")
    for left, right in zip(first, second, strict=True):
        assert (
            json.loads((left.path / demo.MARKER).read_text())["baseline_commit"]
            == (json.loads((right.path / demo.MARKER).read_text())["baseline_commit"])
        )


def test_repeat_preserves_user_changes_branch_and_registration(tmp_path: Path) -> None:
    original = demo.generate(tmp_path)
    root = original[0].path
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    initialized = projects.execute("init", root)
    assert initialized.ok
    config = (root / "jeff.local.yaml").read_bytes()
    (root / "service.py").write_text(demo.SERVICE + "\n# user modification\n")
    (root / "demo-notes.txt").write_text("my changed notes\n")
    git(root, "checkout", "-b", "preserved-work")
    marker = (root / demo.MARKER).read_bytes()
    index = (root / ".git/index").read_bytes()
    repeated = demo.generate(tmp_path)
    assert all(not result.created for result in repeated)
    assert (root / "jeff.local.yaml").read_bytes() == config
    assert (root / "service.py").read_text().endswith("# user modification\n")
    assert (root / "demo-notes.txt").read_text() == "my changed notes\n"
    assert (root / demo.MARKER).read_bytes() == marker
    assert (root / ".git/index").read_bytes() == index
    assert git(root, "branch", "--show-current").stdout.strip() == "preserved-work"
    inspected = projects.execute("config", root)
    assert inspected.ok and inspected.data.project_id == initialized.data.project_id


def test_generated_pytest_failure_is_real_and_expected(tmp_path: Path) -> None:
    root = demo.generate(tmp_path)[0].path
    report = tmp_path / "pytest-report.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_service.py",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
        ],
        cwd=root,
        env={
            "PATH": os.defpath,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    suite = ET.parse(report).getroot().find("testsuite")
    assert suite is not None and suite.get("tests") == "2"
    assert suite.get("failures") == "1" and suite.get("errors") == "0"
    assert "test_status_deliberate_failure" in result.stdout


def test_compose_is_valid_yaml_with_narrow_mount_and_loopback_mapping(tmp_path: Path) -> None:
    root = demo.generate(tmp_path)[0].path
    document = yaml.safe_load((root / "compose.yaml").read_text())
    api = document["services"]["api"]
    assert api["volumes"] == ["./service.py:/app/service.py:ro"]
    assert api["ports"] == ["127.0.0.1::8000"]
    assert api["read_only"] is True and "env_file" not in api
    assert api["image"] == "python:3.12-slim" and "healthcheck" in api
