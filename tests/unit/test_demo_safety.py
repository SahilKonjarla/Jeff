"""Generator preflight and refusal behavior without Git or service execution."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import create_demo_projects as demo


def owned(root: Path, project: str) -> Path:
    target = root / project
    (target / ".git").mkdir(parents=True)
    (target / demo.MARKER).write_text(
        json.dumps(
            {
                "generator": demo.GENERATOR,
                "version": demo.VERSION,
                "project": project,
                "baseline_commit": "a" * 40,
            }
        )
    )
    return target


def test_unrelated_second_target_blocks_all_creation(tmp_path: Path) -> None:
    output = tmp_path / "demos"
    unrelated = output / "second-project"
    unrelated.mkdir(parents=True)
    (unrelated / "user.txt").write_text("preserve me")
    with pytest.raises(demo.DemoError):
        demo.generate(output)
    assert not (output / "sandbox-project").exists()
    assert (unrelated / "user.txt").read_text() == "preserve me"


@pytest.mark.parametrize("kind", ["root", "ancestor", "target", "git", "marker"])
def test_symlinks_are_refused_without_touching_targets(tmp_path: Path, kind: str) -> None:
    output = tmp_path / "demos"
    other = tmp_path / "other"
    other.mkdir()
    target = owned(output, "sandbox-project")
    owned(output, "second-project")
    if kind == "root":
        alias = tmp_path / "alias"
        alias.symlink_to(output, target_is_directory=True)
        output = alias
    elif kind == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        output = alias / "demos"
    elif kind == "target":
        # A missing target name must not adopt a symlink to another directory.
        output = tmp_path / "fresh"
        output.mkdir()
        (output / "sandbox-project").symlink_to(other, target_is_directory=True)
    elif kind == "git":
        (target / ".git").rmdir()
        (target / ".git").symlink_to(other, target_is_directory=True)
    else:
        marker = target / demo.MARKER
        marker.unlink()
        (other / "private.txt").write_text("SYNTHETIC_PRIVATE_VALUE")
        marker.symlink_to(other / "private.txt")
    with pytest.raises(demo.DemoError) as caught:
        demo.generate(output)
    assert "SYNTHETIC_PRIVATE_VALUE" not in str(caught.value)
    assert not (other / "service.py").exists()


@pytest.mark.parametrize(
    "content",
    [
        "not JSON SYNTHETIC_PRIVATE_VALUE",
        "[]",
        "{}",
        "x" * 4097,
        json.dumps(
            {
                "generator": demo.GENERATOR,
                "version": True,
                "project": "sandbox-project",
                "baseline_commit": "a" * 40,
            }
        ),
    ],
)
def test_invalid_markers_are_not_adopted(tmp_path: Path, content: str) -> None:
    target = owned(tmp_path, "sandbox-project")
    (target / demo.MARKER).write_text(content)
    with pytest.raises(demo.DemoError) as caught:
        demo.generate(tmp_path)
    assert "SYNTHETIC_PRIVATE_VALUE" not in str(caught.value)
    assert (target / demo.MARKER).read_text() == content
    assert not (tmp_path / "second-project").exists()


def test_owned_repeated_run_performs_no_git_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for project in demo.PROJECTS:
        target = owned(tmp_path, project)
        (target / "user.txt").write_text("unchanged")

    def unexpected(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("No Git calls on preserved repositories")

    monkeypatch.setattr(demo, "_git", unexpected)
    results = demo.generate(tmp_path)
    assert all(not result.created for result in results)
    assert all((result.path / "user.txt").read_text() == "unchanged" for result in results)


def test_missing_git_fails_before_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    output = tmp_path / "new"
    with pytest.raises(demo.DemoError, match="Install system Git"):
        demo.generate(output)
    assert not output.exists()


def test_parent_traversal_is_rejected_before_creation(tmp_path: Path) -> None:
    with pytest.raises(demo.DemoError, match="traversal"):
        demo.generate(tmp_path / "unused" / ".." / "demos")
    assert not (tmp_path / "demos").exists()


def test_missing_output_parent_is_not_created(tmp_path: Path) -> None:
    output = tmp_path / "missing" / "demos"
    with pytest.raises(demo.DemoError, match="parent"):
        demo.generate(output)
    assert not output.parent.exists()


def test_partial_creation_is_not_repaired_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*args: object, **kwargs: object) -> bytes:
        raise demo.DemoError("Synthetic Git failure")

    monkeypatch.setattr(demo, "_git", failed)
    with pytest.raises(demo.DemoError, match="Synthetic Git failure"):
        demo.generate(tmp_path)
    target = tmp_path / "sandbox-project"
    original = (target / "service.py").read_bytes()
    assert not (target / demo.MARKER).exists()
    with pytest.raises(demo.DemoError):
        demo.generate(tmp_path)
    assert (target / "service.py").read_bytes() == original


def test_git_mutation_cannot_fall_back_to_a_parent_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    (tmp_path / ".git").mkdir()

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("Must refuse before any Git invocation")

    monkeypatch.setattr(subprocess, "run", unexpected)
    with pytest.raises(demo.DemoError):
        demo._git("/unused/git", child, "add", "--", "file")


def test_generated_python_compiles_and_default_location_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    compile(demo.SERVICE, "service.py", "exec")
    compile(demo.TEST_SERVICE, "test_service.py", "exec")
    assert demo.DEFAULT_OUTPUT == Path(demo.__file__).resolve().parents[1] / "examples"
