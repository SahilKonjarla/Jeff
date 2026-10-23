"""Deterministic, project-bound repository questions; no models or execution authority."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from jeff.application.projects import ErrorData, Failure, ProjectService
from jeff.domain.projects import ProjectError
from jeff.domain.repository import BranchState, FileChange, escape_metadata, protected_path


class GitRead(Protocol):
    def read_branch(self, root: Path, *, deadline: float | None = None) -> BranchState: ...
    def read_changes(
        self, root: Path, *, deadline: float | None = None
    ) -> tuple[FileChange, ...]: ...


class AnswerContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    root: str
    observed_at: datetime
    metadata_encoding: Literal["json_escaped"] = "json_escaped"


class BranchData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None
    commit: str | None
    detached: bool
    unborn: bool


class ChangeData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    original_path: str | None
    index_status: str
    worktree_status: str
    staged: bool
    unstaged: bool
    untracked: bool
    conflicted: bool


class BranchAnswer(AnswerContext):
    kind: Literal["branch"] = "branch"
    branch: BranchData
    complete: Literal[True] = True


class ChangesAnswer(AnswerContext):
    kind: Literal["changes"] = "changes"
    changes: list[ChangeData]
    visible_clean: bool
    protected_paths_excluded: Literal[True] = True
    submodule_contents_excluded: Literal[True] = True
    filtered_records: int
    complete: Literal[False] = False


class QuestionSuccess(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: Literal[True] = True
    data: BranchAnswer | ChangesAnswer
    error: None = None


def question_intent(question: str) -> Literal["ask.branch", "ask.changes"] | None:
    intent = " ".join(question.casefold().split()).removesuffix("?").rstrip()
    if intent == "what branch am i on":
        return "ask.branch"
    if intent == "what changed":
        return "ask.changes"
    return None


class QuestionService:
    def __init__(
        self,
        projects: ProjectService,
        git: GitRead,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._projects = projects
        self._git = git
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(
        self, question: str, directory: Path, *, deadline: float | None = None
    ) -> QuestionSuccess | Failure:
        intent = question_intent(question)
        if intent is None:
            return Failure(
                error=ErrorData(
                    code="unsupported_intent",
                    message="Supported questions: 'what branch am I on?' and 'what changed?'.",
                )
            )
        project = self._projects.execute("config", directory, deadline=deadline)
        if not project.ok:
            return project
        root = Path(project.data.root)
        try:
            if intent == "ask.branch":
                branch = self._git.read_branch(
                    root, **({"deadline": deadline} if deadline is not None else {})
                )
                return QuestionSuccess(
                    data=BranchAnswer(
                        project_id=project.data.project_id,
                        root=str(root),
                        observed_at=self._clock(),
                        branch=BranchData(
                            name=escape_metadata(branch.name) if branch.name is not None else None,
                            commit=branch.commit,
                            detached=branch.detached,
                            unborn=branch.unborn,
                        ),
                    )
                )
            raw = self._git.read_changes(
                root, **({"deadline": deadline} if deadline is not None else {})
            )
            visible: list[ChangeData] = []
            for change in raw:
                if protected_path(change.path) or (
                    change.original_path is not None and protected_path(change.original_path)
                ):
                    continue
                visible.append(
                    ChangeData(
                        path=escape_metadata(change.path),
                        original_path=(
                            escape_metadata(change.original_path)
                            if change.original_path is not None
                            else None
                        ),
                        index_status=change.index_status,
                        worktree_status=change.worktree_status,
                        staged=not change.untracked and change.index_status != ".",
                        unstaged=not change.untracked and change.worktree_status != ".",
                        untracked=change.untracked,
                        conflicted=change.conflicted,
                    )
                )
            return QuestionSuccess(
                data=ChangesAnswer(
                    project_id=project.data.project_id,
                    root=str(root),
                    observed_at=self._clock(),
                    changes=visible,
                    visible_clean=not visible,
                    filtered_records=len(raw) - len(visible),
                )
            )
        except ProjectError as error:
            return Failure(error=ErrorData(code=error.code, message=error.message))
