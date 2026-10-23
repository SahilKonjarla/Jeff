"""Narrow read-only repository discovery; no shell or Git mutations."""

import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path

from jeff.domain.projects import ProjectError
from jeff.domain.repository import BranchState, FileChange, git_exclusions

GIT_TIMEOUT = 5.0
GIT_OUTPUT_LIMIT = 1024 * 1024
MAX_CHANGES = 10_000


def _run_git(
    executable: str, directory: Path, *arguments: str, deadline: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Bound both streams while reading; kill/reap only this owned process on failure."""
    command = [
        executable,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "status.submoduleSummary=false",
        "-C",
        str(directory),
        *arguments,
    ]
    environment = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    if deadline is not None and deadline <= time.monotonic():
        raise ProjectError("git_timeout", "Git timed out; no command was retried.")
    try:
        with subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                assert process.stdout is not None and process.stderr is not None
                buffers = {"stdout": bytearray(), "stderr": bytearray()}
                total = 0
                deadline = min(
                    deadline if deadline is not None else float("inf"),
                    time.monotonic() + GIT_TIMEOUT,
                )
                with selectors.DefaultSelector() as selector:
                    for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, selectors.EVENT_READ, label)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, GIT_TIMEOUT)
                        for key, _ in selector.select(min(remaining, 0.1)):
                            chunk = os.read(key.fd, min(65536, GIT_OUTPUT_LIMIT - total + 1))
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            total += len(chunk)
                            if total > GIT_OUTPUT_LIMIT:
                                raise ProjectError(
                                    "git_output_limit",
                                    "Git output exceeded the safety limit. "
                                    "No partial answer is available; reduce the visible changes.",
                                )
                            buffers[str(key.data)].extend(chunk)
                returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                return subprocess.CompletedProcess(
                    command, returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])
                )
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    except subprocess.TimeoutExpired as error:
        raise ProjectError("git_timeout", "Git timed out; no command was retried.") from error
    except OSError as error:
        raise ProjectError(
            "git_failed", "Git could not run; check installation and repository access."
        ) from error


class GitProjectDiscovery:
    def find_root(self, directory: Path, *, deadline: float | None = None) -> Path:
        executable = shutil.which("git", path=os.defpath)
        if executable is None:
            raise ProjectError("git_unavailable", "Install Git before initializing a project.")
        try:
            directory = directory.resolve(strict=True)
            if not directory.is_dir():
                raise ProjectError(
                    "invalid_root", "Run Jeff from an existing repository directory."
                )
            root_text = self._query(
                executable,
                directory,
                "rev-parse",
                "--show-toplevel",
                **({"deadline": deadline} if deadline is not None else {}),
            )
            if root_text.returncode != 0:
                raise ProjectError("not_a_repository", "Run Jeff inside a non-bare Git repository.")
            reported_root = Path(root_text.stdout.removesuffix("\n"))
            if not reported_root.is_absolute():
                raise ProjectError("invalid_root", "Git returned an invalid repository root.")
            root = reported_root.resolve(strict=True)
            if not root.is_dir() or not directory.is_relative_to(root):
                raise ProjectError("invalid_root", "Git returned an invalid repository root.")
            tracked = self._query(
                executable,
                root,
                "ls-files",
                "--",
                "jeff.local.yaml",
                **({"deadline": deadline} if deadline is not None else {}),
            )
            if tracked.returncode != 0:
                raise ProjectError(
                    "git_failed", "Git could not inspect private configuration tracking."
                )
            if tracked.stdout:
                raise ProjectError(
                    "private_config_tracked",
                    "jeff.local.yaml is tracked by Git. Review its tracking manually; "
                    "Jeff will not change the index.",
                )
            return root
        except (OSError, ValueError, RuntimeError, UnicodeError) as error:
            raise ProjectError(
                "discovery_failed", "Repository discovery failed; check Git and directory access."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ProjectError(
                "git_timeout", "Git discovery timed out; no command was retried."
            ) from error

    @staticmethod
    def _query(
        executable: str, directory: Path, *arguments: str, deadline: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = _run_git(
            executable,
            directory,
            *arguments,
            **({"deadline": deadline} if deadline is not None else {}),
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout.decode("utf-8"),
            result.stderr.decode("utf-8", errors="replace"),
        )


def _malformed() -> ProjectError:
    return ProjectError("git_invalid_output", "Git returned unsupported or malformed metadata.")


def _path(raw: bytes) -> str:
    if not raw or raw.startswith(b"/") or any(part in {b".", b".."} for part in raw.split(b"/")):
        raise _malformed()
    return os.fsdecode(raw)


def _parse_status(output: bytes) -> tuple[BranchState, tuple[FileChange, ...]]:
    if not output.endswith(b"\0"):
        raise _malformed()
    records = output[:-1].split(b"\0")
    if len(records) > MAX_CHANGES * 2 + 10:
        raise ProjectError("git_output_limit", "Git returned too many changes; no partial answer.")
    headers: dict[bytes, bytes] = {}
    changes: list[FileChange] = []
    position = 0
    while position < len(records):
        record = records[position]
        position += 1
        if record.startswith(b"# "):
            key, separator, value = record[2:].partition(b" ")
            if not separator or key in headers:
                raise _malformed()
            headers[key] = value
            continue
        if record.startswith(b"? "):
            changes.append(FileChange(_path(record[2:]), None, "?", "?", untracked=True))
        else:
            kind = record[:1]
            count = {b"1": 8, b"2": 9, b"u": 10}.get(kind)
            if count is None:
                raise _malformed()
            fields = record.split(b" ", count)
            if len(fields) != count + 1 or len(fields[1]) != 2:
                raise _malformed()
            xy = fields[1]
            if any(code not in b".MTADRCU" for code in xy):
                raise _malformed()
            original = None
            if kind == b"2":
                if position >= len(records) or not re.fullmatch(
                    rb"[RC](?:100|[0-9]{1,2})", fields[8]
                ):
                    raise _malformed()
                original = _path(records[position])
                position += 1
            changes.append(
                FileChange(
                    _path(fields[-1]),
                    original,
                    chr(xy[0]),
                    chr(xy[1]),
                    conflicted=kind == b"u",
                )
            )
        if len(changes) > MAX_CHANGES:
            raise ProjectError(
                "git_output_limit", "Git returned too many changes; no partial answer."
            )
    head, oid = headers.get(b"branch.head"), headers.get(b"branch.oid")
    if not head or not oid:
        raise _malformed()
    unborn = oid == b"(initial)"
    detached = head == b"(detached)"
    if (unborn and detached) or (
        not unborn and not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid)
    ):
        raise _malformed()
    branch = BranchState(
        name=None if detached else os.fsdecode(head),
        commit=None if unborn else oid.decode("ascii"),
        detached=detached,
        unborn=unborn,
    )
    return branch, tuple(changes)


class GitRepositoryRead:
    def read_branch(self, root: Path, *, deadline: float | None = None) -> BranchState:
        branch, _ = _parse_status(self._status(root, branch_only=True, deadline=deadline))
        return branch

    def read_changes(self, root: Path, *, deadline: float | None = None) -> tuple[FileChange, ...]:
        _, changes = _parse_status(self._status(root, branch_only=False, deadline=deadline))
        return changes

    @staticmethod
    def _status(root: Path, *, branch_only: bool, deadline: float | None = None) -> bytes:
        executable = shutil.which("git", path=os.defpath)
        if executable is None:
            raise ProjectError(
                "git_unavailable", "Install system Git to inspect repository metadata."
            )
        # Status can invoke clean/process drivers while hashing changed files.
        # Discover only effective key names, never command values, then disable drivers.
        filters = _run_git(
            executable,
            root,
            "config",
            "--includes",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|process|required)$",
            **({"deadline": deadline} if deadline is not None else {}),
        )
        if filters.returncode not in {0, 1}:
            raise ProjectError(
                "git_failed", "Git filter configuration could not be checked safely."
            )
        drivers: set[str] = set()
        for key in filters.stdout.split(b"\0"):
            if not key:
                continue
            match = re.fullmatch(rb"filter\.([A-Za-z0-9_.-]+)\.(?:clean|process|required)", key)
            if match is None:
                raise ProjectError(
                    "git_unsafe_config", "Unsupported Git filter name; no status was run."
                )
            drivers.add(match[1].decode("ascii"))
        if len(drivers) > 1000:
            raise ProjectError("git_output_limit", "Too many Git filters to inspect safely.")
        if drivers and not branch_only:
            raise ProjectError(
                "git_filters_unsupported",
                "Repository clean/process filters make status unsafe or misleading to compute "
                "without executing user commands. No change answer was produced.",
            )
        overrides = [
            item
            for driver in sorted(drivers)
            for setting in (
                f"filter.{driver}.clean=",
                f"filter.{driver}.process=",
                f"filter.{driver}.required=false",
            )
            for item in ("-c", setting)
        ]
        exclusions = (":(glob,exclude)**",) if branch_only else git_exclusions()
        result = _run_git(
            executable,
            root,
            *overrides,
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=no" if branch_only else "--untracked-files=all",
            "--ignore-submodules=all",
            "--no-ahead-behind",
            "--no-renames" if branch_only else "--renames",
            "--",
            ".",
            *exclusions,
            **({"deadline": deadline} if deadline is not None else {}),
        )
        if result.returncode != 0:
            raise ProjectError(
                "git_failed", "Git could not inspect this repository; no answer was inferred."
            )
        return result.stdout
