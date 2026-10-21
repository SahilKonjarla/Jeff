"""Real bounded configuration storage and safe repository discovery."""

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.projects import Failure, ProjectService, Success
from jeff.domain.projects import Project
from jeff.infrastructure.project_files import CONFIG_LIMIT, FileProjectStore
from jeff.integrations.git import GitProjectDiscovery


def service() -> ProjectService:
    return ProjectService(GitProjectDiscovery(), FileProjectStore())


def test_initialize_from_subdirectory_and_preserve_settings(git_repo: Path) -> None:
    child = git_repo / "src"
    child.mkdir()
    first = service().execute("init", child)
    assert isinstance(first, Success) and first.data.created
    config = git_repo / "jeff.local.yaml"
    original = config.read_bytes() + b"preferences:\n  theme: dark\n  token: FAKE_PRIVATE_VALUE\n"
    config.write_bytes(original)
    before = config.stat().st_mtime_ns

    repeated = service().execute("init", child)
    inspected = service().execute("config", git_repo)
    status = service().execute("status", child)

    assert isinstance(repeated, Success) and not repeated.data.created
    assert isinstance(inspected, Success) and isinstance(status, Success)
    assert first.data.project_id == repeated.data.project_id == inspected.data.project_id
    assert status.data.registered and status.data.root == str(git_repo)
    assert config.read_bytes() == original and config.stat().st_mtime_ns == before
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert "FAKE_PRIVATE_VALUE" not in inspected.model_dump_json()
    assert "theme" not in inspected.model_dump_json()
    assert not (git_repo / ".jeff").exists()
    assert not list(git_repo.glob(".jeff-write-*.tmp"))


def test_preserve_ignore_contents_and_override_negative_rules(git_repo: Path) -> None:
    ignore = git_repo / ".gitignore"
    original = b"# user rules\n*.cache\n!jeff.local.yaml"
    ignore.write_bytes(original)
    first = service().execute("init", git_repo)
    assert isinstance(first, Success)
    after = ignore.read_bytes()
    assert after.startswith(original + b"\n")
    assert after.endswith(b"/jeff.local.yaml\n/.jeff/\n")
    assert isinstance(service().execute("init", git_repo), Success)
    assert ignore.read_bytes() == after


@pytest.mark.parametrize("operation", ["config", "status"])
def test_inspection_of_unregistered_project_is_read_only(git_repo: Path, operation: str) -> None:
    before = set(git_repo.iterdir())
    result = service().execute("config" if operation == "config" else "status", git_repo)
    assert isinstance(result, Failure) and result.error.code == "project_not_initialized"
    assert set(git_repo.iterdir()) == before


@pytest.mark.parametrize(
    "content",
    [
        "FAKE_PRIVATE_VALUE: [broken",
        "schema_version: 99\nproject: {}",
        "schema_version: true\nproject: {}",
        "schema_version: 1\nproject: {id: invalid, root: /old}",
        "!!python/object/apply:os.system ['echo FAKE_PRIVATE_VALUE']",
        "value: &anchor [*anchor]",
        "[1, 2, 3]",
        "value: " + "[" * 25 + "0" + "]" * 25,
    ],
)
def test_invalid_config_is_not_replaced_or_leaked(git_repo: Path, content: str) -> None:
    config = git_repo / "jeff.local.yaml"
    config.write_text(content)
    result = service().execute("init", git_repo)
    assert isinstance(result, Failure) and result.error.code == "config_invalid"
    assert "FAKE_PRIVATE_VALUE" not in result.model_dump_json()
    assert config.read_text() == content
    assert not (git_repo / ".gitignore").exists()


def test_oversized_config_is_rejected(git_repo: Path) -> None:
    (git_repo / "jeff.local.yaml").write_bytes(b"x" * (CONFIG_LIMIT + 1))
    result = service().execute("config", git_repo)
    assert isinstance(result, Failure) and result.error.code == "file_too_large"


@pytest.mark.parametrize("name", ["jeff.local.yaml", ".gitignore", ".jeff"])
def test_unsafe_symlinks_are_neither_followed_nor_overwritten(
    git_repo: Path, tmp_path: Path, name: str
) -> None:
    target = tmp_path / "private.txt"
    target.write_text("FAKE_PRIVATE_VALUE")
    (git_repo / name).symlink_to(target)
    result = service().execute("init", git_repo)
    assert isinstance(result, Failure) and result.error.code == "unsafe_path"
    assert "FAKE_PRIVATE_VALUE" not in result.model_dump_json()
    assert target.read_text() == "FAKE_PRIVATE_VALUE"
    assert (git_repo / name).is_symlink()


def test_hardlinked_config_is_rejected(git_repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "private.txt"
    target.write_text("FAKE_PRIVATE_VALUE")
    os.link(target, git_repo / "jeff.local.yaml")
    result = service().execute("config", git_repo)
    assert isinstance(result, Failure) and result.error.code == "unsafe_path"


def test_special_config_is_rejected_without_opening_fifo(git_repo: Path) -> None:
    os.mkfifo(git_repo / "jeff.local.yaml")
    result = service().execute("config", git_repo)
    assert isinstance(result, Failure) and result.error.code == "unsafe_path"


def test_moved_project_requires_explicit_rebind(git_repo: Path) -> None:
    original = (
        f"schema_version: 1\nproject:\n  id: {uuid4()}\n"
        f"  root: {json.dumps(str(git_repo / 'previous'))}\n"
    )
    config = git_repo / "jeff.local.yaml"
    config.write_text(original)
    result = service().execute("init", git_repo)
    assert isinstance(result, Failure) and result.error.code == "project_moved"
    assert config.read_text() == original
    assert not (git_repo / ".gitignore").exists()


def test_filesystem_failure_is_safe_and_cleans_temporary_files(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny_replace(*args: object, **kwargs: object) -> None:
        raise PermissionError("FAKE_PRIVATE_VALUE")

    ignore = git_repo / ".gitignore"
    ignore.write_text("# original\n")
    monkeypatch.setattr(os, "replace", deny_replace)
    result = service().execute("init", git_repo)
    assert isinstance(result, Failure) and result.error.code == "storage_io"
    assert "FAKE_PRIVATE_VALUE" not in result.model_dump_json()
    assert ignore.read_text() == "# original\n"
    assert not (git_repo / "jeff.local.yaml").exists()
    assert not list(git_repo.glob(".jeff-write-*.tmp"))


def test_config_write_failure_reports_partial_ignore_state(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link

    def deny_config(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if destination == "jeff.local.yaml":
            raise PermissionError("FAKE_PRIVATE_VALUE")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", deny_config)
    result = service().execute("init", git_repo)
    assert isinstance(result, Failure) and result.error.code == "storage_io"
    assert "may have added ignore rules" in result.error.message
    assert "FAKE_PRIVATE_VALUE" not in result.model_dump_json()
    assert (git_repo / ".gitignore").exists()
    assert not (git_repo / "jeff.local.yaml").exists()
    assert not list(git_repo.glob(".jeff-write-*.tmp"))


def test_detected_concurrent_ignore_edit_is_preserved(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ignore = git_repo / ".gitignore"
    ignore.write_text("# original\n")
    real_fsync = os.fsync

    def edit_during_write(descriptor: int) -> None:
        real_fsync(descriptor)
        ignore.write_text("# concurrent user edit\n")

    monkeypatch.setattr(os, "fsync", edit_during_write)
    result = service().execute("init", git_repo)
    assert isinstance(result, Failure) and result.error.code == "concurrent_change"
    assert ignore.read_text() == "# concurrent user edit\n"
    assert not (git_repo / "jeff.local.yaml").exists()
    assert not list(git_repo.glob(".jeff-write-*.tmp"))


def test_real_project_move_does_not_silently_rebind(git_repo: Path) -> None:
    first = service().execute("init", git_repo)
    assert isinstance(first, Success)
    original = (git_repo / "jeff.local.yaml").read_bytes()
    moved = git_repo.with_name("moved project")
    git_repo.rename(moved)
    result = service().execute("init", moved)
    assert isinstance(result, Failure) and result.error.code == "project_moved"
    assert (moved / "jeff.local.yaml").read_bytes() == original


def test_initialization_never_overwrites_concurrently_created_config(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project(uuid4(), git_repo)
    winner = Project(uuid4(), git_repo)
    real_link = os.link

    def race(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if destination == "jeff.local.yaml":
            (git_repo / destination).write_text(
                f"schema_version: 1\nproject:\n  id: {winner.project_id}\n"
                f"  root: {json.dumps(str(git_repo))}\n"
            )
            raise FileExistsError()
        # Test uses the same supported dir-fd arguments as production.
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", race)
    actual, created = FileProjectStore().initialize(project)
    assert actual == winner and not created
    assert not list(git_repo.glob(".jeff-write-*.tmp"))
