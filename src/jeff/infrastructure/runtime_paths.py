"""Explicit Mac runtime location selection; no environment reading or state creation."""

import os
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from jeff.application.projects import Failure
from jeff.application.runtime import (
    MAC_NAME_BYTES,
    MAC_PATH_BYTES,
    MAC_SOCKET_PATH_BYTES,
    PathObservation,
    RuntimeIssueCode,
    RuntimeLayout,
    RuntimePathInputs,
    RuntimePathInspection,
    RuntimePathRole,
    runtime_failure,
)


def _absolute(value: str) -> Path:
    if not value or "\0" in value:
        raise ValueError("Invalid location")
    value.encode("utf-8")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or value.startswith("//"):
        raise ValueError("Invalid location")
    # Known Darwin system aliases only, not arbitrary Path.resolve()/leaf symlink following.
    for alias in ("tmp", "var", "etc"):
        if path.parts[1:2] == (alias,):
            return Path("/private") / path.relative_to("/")
    return path


def resolve_layout(inputs: RuntimePathInputs) -> RuntimeLayout | Failure:
    """Pure location selection from trusted explicit inputs; no host/env/filesystem reads."""
    if inputs.platform != "darwin":
        return runtime_failure("unsupported_runtime_platform")
    if type(inputs.uid) is not int or inputs.uid < 0:
        return runtime_failure("invalid_runtime_path")
    try:
        home = _absolute(str(inputs.home))
        temporary = _absolute(str(inputs.temp_dir))
        data = (
            _absolute(inputs.data_dir)
            if inputs.data_dir is not None
            else home / "Library" / "Application Support" / "Jeff"
        )
        runtime = (
            _absolute(inputs.runtime_dir)
            if inputs.runtime_dir is not None
            else temporary / f"jeff-{inputs.uid}"
        )
        layout = RuntimeLayout(data_dir=data, runtime_dir=runtime, uid=inputs.uid)
        issue = _path_issue(layout)
        return runtime_failure(issue) if issue else layout
    except (ValueError, TypeError):
        return runtime_failure("invalid_runtime_path")


def _path_issue(layout: RuntimeLayout) -> RuntimeIssueCode | None:
    paths = (layout.database, layout.settings, layout.credential, layout.socket, layout.lock)
    if len(os.fsencode(layout.socket)) > MAC_SOCKET_PATH_BYTES or any(
        len(os.fsencode(path)) > MAC_PATH_BYTES
        or any(len(os.fsencode(part)) > MAC_NAME_BYTES for part in path.parts)
        for path in paths
    ):
        return "runtime_path_too_long"
    return None


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid, metadata.st_nlink)


def _unavailable(
    role: RuntimePathRole,
    children: tuple[tuple[RuntimePathRole, str], ...],
    state: Literal["missing", "unsafe", "unknown"],
    issue: RuntimeIssueCode | None = None,
) -> list[PathObservation]:
    return [PathObservation(role=role, state=state, issue=issue)] + [
        PathObservation(
            role=child_role,
            state="missing" if state == "missing" else "unknown",
            issue=issue,
        )
        for child_role, _ in children
    ]


def _inspect_file(fd: int, name: str, role: RuntimePathRole, uid: int) -> PathObservation:
    try:
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return PathObservation(role=role, state="missing")
    except OSError:
        return PathObservation(role=role, state="unknown", issue="runtime_inspection_failed")
    expected_type = stat.S_ISSOCK if role == "socket" else stat.S_ISREG
    if not (
        expected_type(before.st_mode)
        and before.st_uid == uid
        and stat.S_IMODE(before.st_mode) == 0o600
        and before.st_nlink == 1
    ):
        return PathObservation(role=role, state="unsafe", issue="unsafe_runtime_path")
    try:
        after = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if _signature(before) != _signature(after):
            raise OSError("Metadata changed")
    except OSError:
        return PathObservation(role=role, state="unknown", issue="runtime_inspection_failed")
    return PathObservation(role=role, state="present")


def _chain_stable(chain: list[tuple[int, str, os.stat_result]]) -> bool:
    try:
        return all(
            _signature(before) == _signature(os.stat(name, dir_fd=fd, follow_symlinks=False))
            for fd, name, before in chain
        )
    except OSError:
        return False


def _inspect_group(
    directory: Path,
    role: RuntimePathRole,
    children: tuple[tuple[RuntimePathRole, str], ...],
    uid: int,
    private_directories: tuple[Path, Path],
) -> list[PathObservation]:
    # Traverse canonical absolute paths through no-follow directory descriptors. Never open
    # a state file, and never traverse a Jeff directory rejected by this inspection.
    try:
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open("/", flags)
            stack.callback(os.close, fd)
            chain: list[tuple[int, str, os.stat_result]] = []
            current = Path("/")
            for component in directory.parts[1:]:
                try:
                    before = os.stat(component, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    if _chain_stable(chain):
                        return _unavailable(role, children, "missing")
                    return _unavailable(role, children, "unknown", "runtime_inspection_failed")
                current /= component
                if not stat.S_ISDIR(before.st_mode) or (
                    current in private_directories
                    and (before.st_uid != uid or stat.S_IMODE(before.st_mode) != 0o700)
                ):
                    return _unavailable(role, children, "unsafe", "unsafe_runtime_path")
                next_fd = os.open(component, flags, dir_fd=fd)
                stack.callback(os.close, next_fd)
                if _signature(before) != _signature(os.fstat(next_fd)):
                    return _unavailable(role, children, "unknown", "runtime_inspection_failed")
                chain.append((fd, component, before))
                fd = next_fd
            metadata = os.fstat(fd)
            if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) != 0o700:
                return _unavailable(role, children, "unsafe", "unsafe_runtime_path")
            entries = [PathObservation(role=role, state="present")]
            entries.extend(
                _inspect_file(fd, name, child_role, uid) for child_role, name in children
            )
            # Detect observed ancestor/leaf replacements before handing back a snapshot.
            if not _chain_stable(chain):
                return _unavailable(role, children, "unknown", "runtime_inspection_failed")
            return entries
    except OSError:
        return _unavailable(role, children, "unknown", "runtime_inspection_failed")


def inspect_layout(layout: RuntimeLayout) -> RuntimePathInspection | Failure:
    """Read only fixed no-follow metadata. Missing is not stopped; present is not owned/live.

    This snapshot cannot authorize a later open/create/unlink without fresh validation.
    Expected UID and layout must come from trusted host selection, not an IPC client.
    """
    try:
        issue = _path_issue(layout)
        if issue:
            return runtime_failure(issue)
    except (ValueError, TypeError):
        return runtime_failure("invalid_runtime_path")
    private_directories = (layout.data_dir, layout.runtime_dir)
    entries = _inspect_group(
        layout.data_dir,
        "data_dir",
        (
            ("database", "state.sqlite3"),
            ("settings", "settings.yaml"),
            ("credential", "ipc.key"),
        ),
        layout.uid,
        private_directories,
    )
    entries.extend(
        _inspect_group(
            layout.runtime_dir,
            "runtime_dir",
            (
                ("socket", "daemon.sock"),
                ("lock", "daemon.lock"),
            ),
            layout.uid,
            private_directories,
        )
    )
    return RuntimePathInspection(entries=tuple(entries))
