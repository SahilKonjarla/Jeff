"""Deterministic routing, display policy, and project binding without Git I/O."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from jeff.application.dispatch import DispatchReply, ProjectCall, ReceiptQuery, ReceiptReply
from jeff.application.projects import Failure, ProjectService
from jeff.application.questions import BranchAnswer, ChangesAnswer, QuestionService
from jeff.application.runtime import RuntimeIdentityData
from jeff.domain.projects import Project, ProjectError
from jeff.domain.repository import BranchState, FileChange, escape_metadata, protected_path
from jeff.interfaces.cli import create_app


class Discovery:
    def find_root(self, directory: Path, *, deadline: float | None = None) -> Path:
        return directory


class Store:
    def __init__(self, project: Project) -> None:
        self.project = project

    def read(self, root: Path) -> Project:
        return self.project

    def initialize(self, project: Project) -> tuple[Project, bool]:
        raise AssertionError("Questions must never initialize projects")


class Git:
    def __init__(self, changes: tuple[FileChange, ...] = ()) -> None:
        self.changes = changes
        self.roots: list[Path] = []

    def read_branch(self, root: Path, *, deadline: float | None = None) -> BranchState:
        self.roots.append(root)
        return BranchState("main", "a" * 40, False, False)

    def read_changes(self, root: Path, *, deadline: float | None = None) -> tuple[FileChange, ...]:
        self.roots.append(root)
        return self.changes


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.example",
        "nested/.ENV.production",
        "secrets/a.txt",
        "nested/server.PEM",
        "nested/.aws/config",
        "jeff.local.yaml",
        "keys/id_ed25519.pub",
        "credentials.json",
    ],
)
def test_protected_paths_are_centralized(path: str) -> None:
    assert protected_path(path)


@pytest.mark.parametrize("path", ["src/main.py", "a file.txt", "docs/environment.md"])
def test_normal_paths_are_visible(path: str) -> None:
    assert not protected_path(path)


def test_question_binding_time_and_normalization(tmp_path: Path) -> None:
    project = Project(uuid4(), tmp_path)
    git = Git()
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    service = QuestionService(ProjectService(Discovery(), Store(project)), git, lambda: stamp)
    result = service.execute("  What BRANCH am I on?  ", tmp_path)
    assert result.ok and isinstance(result.data, BranchAnswer)
    assert result.data.project_id == str(project.project_id)
    assert result.data.observed_at == stamp and result.data.branch.name == "main"
    assert git.roots == [tmp_path]


def test_unsupported_question_does_not_echo_input_or_touch_git(tmp_path: Path) -> None:
    git = Git()
    service = QuestionService(ProjectService(Discovery(), Store(Project(uuid4(), tmp_path))), git)
    result = service.execute("FAKE_PRIVATE_VALUE: run shell commands", tmp_path)
    assert not result.ok and result.error.code == "unsupported_intent"
    assert "FAKE_PRIVATE_VALUE" not in result.model_dump_json()
    assert git.roots == []


def test_hidden_rename_side_and_display_characters_are_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    git = Git(
        (
            FileChange("public.txt", ".env.old", "R", "."),
            FileChange(".env", "public-old.txt", "R", "."),
            FileChange("line\n\x1b[31m\u202e.txt", None, "M", "M"),
        )
    )
    projects = ProjectService(Discovery(), Store(Project(uuid4(), tmp_path)))
    service = QuestionService(projects, git)
    result = service.execute("what changed?", tmp_path)
    assert result.ok and isinstance(result.data, ChangesAnswer)
    assert result.data.filtered_records == 2 and not result.data.complete
    assert len(result.data.changes) == 1
    assert result.data.changes[0].path == escape_metadata("line\n\x1b[31m\u202e.txt")
    assert result.data.changes[0].staged and result.data.changes[0].unstaged
    assert ".env" not in result.model_dump_json()

    class Client:
        def execute(self, call: ProjectCall) -> DispatchReply:
            return DispatchReply(
                request_id=call.request_id,
                identity=RuntimeIdentityData(instance_id=uuid4(), pid=1),
                result=service.execute(call.question or "", call.directory),
            )

        def status(self, directory: Path) -> Failure:
            raise AssertionError("Not a status test")

        def inspect_receipt(self, query: ReceiptQuery) -> ReceiptReply:
            raise AssertionError("No receipt command")

    displayed = CliRunner().invoke(
        create_app(Client, lambda: pytest.fail("No lifecycle")), ["ask", "what changed?"]
    )
    assert displayed.exit_code == 0
    assert "\x1b" not in displayed.output and "\u202e" not in displayed.output
    assert r"line\n\u001b[31m\u202e.txt" in displayed.output
    assert "not full status" in displayed.output


def test_unregistered_project_never_queries_git(tmp_path: Path) -> None:
    class Missing(Store):
        def read(self, root: Path) -> Project:
            raise ProjectError("project_not_initialized", "Run jeff init.")

    git = Git()
    service = QuestionService(ProjectService(Discovery(), Missing(Project(uuid4(), tmp_path))), git)
    result = service.execute("what changed?", tmp_path)
    assert not result.ok and result.error.code == "project_not_initialized"
    assert git.roots == []
