"""Fast checks of the CLI's currently implemented public interface."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jeff.application.projects import ProjectService
from jeff.application.questions import QuestionService
from jeff.domain.projects import Project
from jeff.domain.repository import BranchState, FileChange
from jeff.interfaces.cli import create_app


class UnusedDiscovery:
    def find_root(self, directory: Path) -> Path:
        raise AssertionError("Help must not discover repositories")


class UnusedStore:
    def initialize(self, project: Project) -> tuple[Project, bool]:
        raise AssertionError("Help must not write configuration")

    def read(self, root: Path) -> Project:
        raise AssertionError("Help must not read configuration")


class UnusedGit:
    def read_branch(self, root: Path) -> BranchState:
        raise AssertionError("Help must not query Git")

    def read_changes(self, root: Path) -> tuple[FileChange, ...]:
        raise AssertionError("Help must not query Git")


projects = ProjectService(UnusedDiscovery(), UnusedStore())
app = create_app(projects, QuestionService(projects, UnusedGit()))
runner = CliRunner()


def test_help_is_honest_and_has_no_filesystem_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Local developer assistant" in result.output
    assert "Project registration" in result.output
    assert all(command in result.output for command in ("init", "config", "status"))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["start", "run"])
def test_unimplemented_commands_are_rejected(command: str) -> None:
    result = runner.invoke(app, [command])

    assert result.exit_code == 2
    assert "No such command" in result.output


def test_no_arguments_shows_usage_without_claiming_success() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 2
    assert "Usage:" in result.output
