"""Bounded, no-follow project configuration and atomic local writes."""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import yaml  # type: ignore[import-untyped]

from jeff.domain.projects import Project, ProjectError

CONFIG_NAME = "jeff.local.yaml"
CONFIG_LIMIT = 64 * 1024
IGNORE_LIMIT = 1024 * 1024
IGNORE_RULES = b"/jeff.local.yaml\n/.jeff/\n"


def _unsafe() -> ProjectError:
    return ProjectError(
        "unsafe_path",
        "Jeff configuration, ignore files, and runtime paths must not be unsafe links "
        "or special files. Review these paths manually.",
    )


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


class FileProjectStore:
    def initialize(self, project: Project) -> tuple[Project, bool]:
        with self._root(project.root) as directory:
            existing = self._load(directory, project.root)
            self._ensure_ignored(directory)
            if existing is not None:
                return existing, False
            payload = yaml.safe_dump(
                {
                    "schema_version": 1,
                    "project": {"id": str(project.project_id), "root": str(project.root)},
                },
                sort_keys=False,
            ).encode("utf-8")
            try:
                self._write(directory, CONFIG_NAME, payload, replace=False)
            except FileExistsError:
                # Another initialization won. Never overwrite or adopt invalid state.
                winner = self._load(directory, project.root)
                if winner is None:
                    raise ProjectError(
                        "concurrent_change",
                        "Project configuration changed concurrently; inspect it.",
                    ) from None
                return winner, False
            return project, True

    def read(self, root: Path) -> Project:
        with self._root(root) as directory:
            project = self._load(directory, root)
            if project is None:
                raise ProjectError(
                    "project_not_initialized", "This repository is not registered. Run jeff init."
                )
            return project

    @staticmethod
    @contextmanager
    def _root(root: Path) -> Iterator[int]:
        directory: int | None = None
        try:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                runtime = os.stat(".jeff", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISDIR(runtime.st_mode):
                    raise _unsafe()
            yield directory
        except OSError as error:
            raise ProjectError(
                "storage_io",
                "Project storage failed; check directory permissions and unsafe paths. "
                "Initialization may have added ignore rules; inspect before retrying.",
            ) from error
        finally:
            if directory is not None:
                os.close(directory)

    @staticmethod
    def _read(directory: int, name: str, limit: int) -> tuple[bytes, os.stat_result] | None:
        try:
            entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise _unsafe()
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
            ):
                raise _unsafe()
            if before.st_size > limit:
                raise ProjectError(
                    "file_too_large", "Project configuration or ignore file is too large."
                )
            content = stream.read(limit + 1)
            if len(content) > limit or _fingerprint(before) != _fingerprint(
                os.fstat(stream.fileno())
            ):
                raise ProjectError(
                    "concurrent_change",
                    "Project files changed while reading; inspect before retrying.",
                )
        return content, before

    def _load(self, directory: int, root: Path) -> Project | None:
        result = self._read(directory, CONFIG_NAME, CONFIG_LIMIT)
        if result is None:
            return None
        try:
            text = result[0].decode("utf-8")
            depth = 0
            for count, event in enumerate(yaml.parse(text), start=1):
                if count > 4096 or isinstance(event, yaml.events.AliasEvent):
                    raise ValueError("Unsupported YAML structure")
                if getattr(event, "anchor", None) is not None:
                    raise ValueError("YAML anchors are not supported")
                if isinstance(event, yaml.events.CollectionStartEvent):
                    depth += 1
                    if depth > 20:
                        raise ValueError("YAML nesting limit exceeded")
                elif isinstance(event, yaml.events.CollectionEndEvent):
                    depth -= 1
            document: object = yaml.safe_load(text)
            if not isinstance(document, dict):
                raise ValueError("Expected mapping")
            version = document.get("schema_version")
            identity = document.get("project")
            if type(version) is not int or version != 1 or not isinstance(identity, dict):
                raise ValueError("Invalid schema")
            project_id, configured_root = identity.get("id"), identity.get("root")
            if not isinstance(project_id, str) or not isinstance(configured_root, str):
                raise ValueError("Invalid identity")
            if not Path(configured_root).is_absolute():
                raise ValueError("Root is not absolute")
            project = Project(UUID(project_id), Path(configured_root))
        except (yaml.YAMLError, UnicodeError, ValueError, RecursionError) as error:
            raise ProjectError(
                "config_invalid",
                "jeff.local.yaml is invalid or unsupported. Review its schema "
                "and structure; existing contents were not replaced.",
            ) from error
        if project.root != root:
            raise ProjectError(
                "project_moved",
                "The configured root differs from this repository. "
                "Project rebind is not implemented; no identity was changed.",
            )
        return project

    def _ensure_ignored(self, directory: int) -> None:
        result = self._read(directory, ".gitignore", IGNORE_LIMIT)
        original, expected = result if result is not None else (b"", None)
        if original.splitlines()[-2:] == IGNORE_RULES.splitlines():
            return
        separator = b"" if not original or original.endswith(b"\n") else b"\n"
        payload = original + separator + b"\n# Jeff private project state\n" + IGNORE_RULES
        self._write(directory, ".gitignore", payload, replace=True, expected=expected)

    @staticmethod
    def _write(
        directory: int,
        name: str,
        content: bytes,
        *,
        replace: bool,
        expected: os.stat_result | None = None,
    ) -> None:
        temporary = f".jeff-write-{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                if replace:
                    os.fchmod(
                        stream.fileno(), stat.S_IMODE(expected.st_mode) if expected else 0o644
                    )
                os.fsync(stream.fileno())
            if not replace:
                # Link creation is atomic and refuses to replace any existing entry.
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                return
            try:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if (current is None) != (expected is None) or (
                current is not None
                and expected is not None
                and _fingerprint(current) != _fingerprint(expected)
            ):
                raise ProjectError(
                    "concurrent_change",
                    "Ignore rules changed concurrently; no replacement was made.",
                )
            if expected is None:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            else:
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
