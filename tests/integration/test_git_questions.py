"""Real Git metadata, scoped path exclusions, bounded subprocesses, no query writes."""

import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from jeff.application.projects import ProjectService
from jeff.application.questions import BranchAnswer, ChangesAnswer, QuestionService
from jeff.domain.projects import ProjectError
from jeff.domain.repository import escape_metadata, protected_path
from jeff.infrastructure.project_files import FileProjectStore
from jeff.integrations import git as adapter
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead

GitRun = Callable[..., subprocess.CompletedProcess[bytes]]


@pytest.fixture
def questions(git_repo: Path, git_run: GitRun) -> QuestionService:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    (git_repo / "sample.txt").write_text("baseline\n")
    (git_repo / "old.txt").write_text("distinct rename source\n")
    git_run("add", "--", ".gitignore", "sample.txt", "old.txt")
    git_run("commit", "-m", "Fixture baseline")
    return QuestionService(projects, GitRepositoryRead())


def test_unborn_branch(git_repo: Path) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    result = QuestionService(projects, GitRepositoryRead()).execute(
        "what branch am I on?", git_repo
    )
    assert result.ok and isinstance(result.data, BranchAnswer)
    assert result.data.branch.name == "main" and result.data.branch.unborn
    assert result.data.branch.commit is None and not result.data.branch.detached


def test_named_clean_and_detached_states(
    git_repo: Path, git_run: GitRun, questions: QuestionService
) -> None:
    branch = questions.execute("what branch am I on?", git_repo)
    assert branch.ok and isinstance(branch.data, BranchAnswer)
    assert branch.data.branch.name == "main" and not branch.data.branch.unborn
    changes = questions.execute("what changed?", git_repo)
    assert changes.ok and isinstance(changes.data, ChangesAnswer)
    assert changes.data.visible_clean and not changes.data.complete
    git_run("checkout", "--detach", "HEAD")
    detached = questions.execute("what branch am I on?", git_repo)
    assert detached.ok and isinstance(detached.data, BranchAnswer)
    assert detached.data.branch.detached and detached.data.branch.name is None
    assert detached.data.branch.commit == branch.data.branch.commit


def test_staged_unstaged_untracked_renamed_and_read_only(
    git_repo: Path, git_run: GitRun, questions: QuestionService
) -> None:
    sample = git_repo / "sample.txt"
    sample.write_text("staged\n")
    git_run("add", "--", "sample.txt")
    sample.write_text("unstaged\n")
    (git_repo / "new file.txt").write_text("untracked\n")
    (git_repo / "old.txt").rename(git_repo / "renamed.txt")
    git_run("add", "--", "old.txt", "renamed.txt")
    index = (git_repo / ".git/index").read_bytes()
    head = (git_repo / ".git/HEAD").read_bytes()
    contents = sample.read_bytes()
    result = questions.execute("what changed?", git_repo)
    assert result.ok and isinstance(result.data, ChangesAnswer)
    changes = {change.path: change for change in result.data.changes}
    assert changes["sample.txt"].staged and changes["sample.txt"].unstaged
    assert changes["new file.txt"].untracked and not changes["new file.txt"].staged
    assert changes["renamed.txt"].original_path == "old.txt"
    assert changes["renamed.txt"].index_status == "R"
    assert (git_repo / ".git/index").read_bytes() == index
    assert (git_repo / ".git/HEAD").read_bytes() == head and sample.read_bytes() == contents
    assert not (git_repo / ".git/index.lock").exists()


def test_conflict_status(git_repo: Path, git_run: GitRun, questions: QuestionService) -> None:
    git_run("checkout", "-b", "side")
    (git_repo / "sample.txt").write_text("side\n")
    git_run("add", "sample.txt")
    git_run("commit", "-m", "Side fixture")
    git_run("checkout", "main")
    (git_repo / "sample.txt").write_text("main\n")
    git_run("add", "sample.txt")
    git_run("commit", "-m", "Main fixture")
    assert git_run("merge", "--no-edit", "side", check=False).returncode == 1
    result = questions.execute("what changed?", git_repo)
    assert result.ok and isinstance(result.data, ChangesAnswer)
    conflict = next(change for change in result.data.changes if change.path == "sample.txt")
    assert conflict.conflicted and conflict.index_status == conflict.worktree_status == "U"


def test_protected_paths_excluded_and_unusual_names_escaped(
    git_repo: Path, git_run: GitRun, questions: QuestionService
) -> None:
    # Synthetic environment fixture, never a real credential or user .env file.
    (git_repo / ".env.production").write_text("FAKE_TEST_SENTINEL")
    git_run("add", "-f", "--", ".env.production")
    git_run("commit", "-m", "Synthetic protected fixture")
    (git_repo / ".env.production").write_text("FAKE_CHANGED_SENTINEL")
    (git_repo / ".env.example").write_text("FAKE_TEMPLATE")
    (git_repo / "secrets").mkdir()
    (git_repo / "secrets/private.txt").write_text("FAKE_TEST_SENTINEL")
    nested = git_repo / "nested"
    (nested / ".AWS").mkdir(parents=True)
    (nested / ".AWS/config").write_text("FAKE_TEST_SENTINEL")
    (nested / "service.KEY").write_text("FAKE_TEST_SENTINEL")
    (git_repo / "line\n\x1b[31m.txt").write_text("visible fixture")
    raw = GitRepositoryRead().read_changes(git_repo)
    assert raw and all(not protected_path(change.path) for change in raw)
    result = questions.execute("what changed?", git_repo)
    assert result.ok and isinstance(result.data, ChangesAnswer)
    assert [change.path for change in result.data.changes] == [r"line\n\u001b[31m.txt"]
    assert result.data.protected_paths_excluded and not result.data.complete
    assert ".env" not in result.model_dump_json() and "SENTINEL" not in result.model_dump_json()


def test_repository_filters_cannot_execute_during_queries(
    git_repo: Path, git_run: GitRun, questions: QuestionService
) -> None:
    marker = git_repo / "filter-was-executed"
    command = f"touch {shlex.quote(str(marker))}; cat"
    git_run("config", "filter.probe.clean", command)
    git_run("config", "filter.probe.process", command)
    git_run("config", "filter.probe.required", "true")
    (git_repo / ".gitattributes").write_text("*.txt filter=probe\n")
    (git_repo / "sample.txt").write_text("changed\n")
    before = (git_repo / ".git/config").read_bytes()
    branch = questions.execute("what branch am I on?", git_repo)
    assert branch.ok and isinstance(branch.data, BranchAnswer)
    changed = questions.execute("what changed?", git_repo)
    assert not changed.ok and changed.error.code == "git_filters_unsupported"
    assert not marker.exists()
    assert (git_repo / ".git/config").read_bytes() == before


def test_real_output_limit_is_a_failure_not_a_partial_answer(
    git_repo: Path, questions: QuestionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(20):
        (git_repo / f"file-{index}-long-name.txt").touch()
    monkeypatch.setattr(adapter, "GIT_OUTPUT_LIMIT", 256)
    result = questions.execute("what changed?", git_repo)
    assert not result.ok and result.error.code == "git_output_limit"
    assert result.data is None


def test_real_timeout_kills_and_reports_owned_process(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "GIT_TIMEOUT", 0.0)
    with pytest.raises(ProjectError) as caught:
        GitRepositoryRead().read_changes(git_repo)
    assert caught.value.code == "git_timeout"


@pytest.mark.parametrize(
    "output",
    [
        b"FAKE_TEST_SENTINEL",
        b"# branch.head main\0",
        b"? ../outside\0",
        b"# branch.head main\0# branch.oid (initial)\0? /outside\0",
    ],
)
def test_malformed_metadata_is_not_rendered(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, output: bytes
) -> None:
    def fake(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if args[2] == "config":
            return subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess([], 0, stdout=output, stderr=b"")

    monkeypatch.setattr(adapter, "_run_git", fake)
    with pytest.raises(ProjectError) as caught:
        GitRepositoryRead().read_changes(git_repo)
    assert caught.value.code == "git_invalid_output"
    assert "SENTINEL" not in caught.value.message


def test_stderr_limit_kills_and_reaps_the_owned_child(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(
        command: list[str], *, env: dict[str, str], stdin: int, stdout: int, stderr: int
    ) -> subprocess.Popen[bytes]:
        assert env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_NO_LAZY_FETCH"] == "1"
        child = real_popen(
            [
                sys.executable,
                "-c",
                "import sys,time; "
                "sys.stderr.buffer.write(b'x'*20000); sys.stderr.flush(); time.sleep(10)",
            ],
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(adapter, "GIT_OUTPUT_LIMIT", 1024)
    with pytest.raises(ProjectError) as caught:
        GitRepositoryRead().read_changes(git_repo)
    assert caught.value.code == "git_output_limit"
    assert len(children) == 1 and children[0].poll() is not None


def test_non_utf8_filename_has_reversible_safe_encoding(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = b"# branch.head main\0# branch.oid (initial)\0? bad-\xff.txt\0"

    def fake(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if args[2] == "config":
            return subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess([], 0, stdout=output, stderr=b"")

    monkeypatch.setattr(adapter, "_run_git", fake)
    changes = GitRepositoryRead().read_changes(git_repo)
    assert escape_metadata(changes[0].path) == r"bad-\udcff.txt"


def test_git_failure_does_not_echo_stderr(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 128, stdout=b"", stderr=b"FAKE_TEST_SENTINEL")

    monkeypatch.setattr(adapter, "_run_git", fake)
    with pytest.raises(ProjectError) as caught:
        GitRepositoryRead().read_branch(git_repo)
    assert caught.value.code == "git_failed" and "SENTINEL" not in caught.value.message
