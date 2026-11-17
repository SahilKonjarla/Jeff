"""Strict bounded porcelain parsing; malformed output never becomes a partial answer."""

import os
import re

from jeff.domain.projects import ProjectError
from jeff.domain.repository import BranchState, FileChange

from .. import git as adapter


def _malformed() -> ProjectError:
    return ProjectError("git_invalid_output", "Git returned unsupported or malformed metadata.")


def _path(raw: bytes) -> str:
    if not raw or raw.startswith(b"/") or any(part in {b".", b".."} for part in raw.split(b"/")):
        raise adapter._malformed()
    return os.fsdecode(raw)


def _parse_status(output: bytes) -> tuple[BranchState, tuple[FileChange, ...]]:
    """Parse NUL records, including rename pairs, or reject the entire bounded answer."""
    if not output.endswith(b"\0"):
        raise adapter._malformed()
    records = output[:-1].split(b"\0")
    if len(records) > adapter.MAX_CHANGES * 2 + 10:
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
                raise adapter._malformed()
            headers[key] = value
            continue
        if record.startswith(b"? "):
            changes.append(FileChange(adapter._path(record[2:]), None, "?", "?", untracked=True))
        else:
            kind = record[:1]
            count = {b"1": 8, b"2": 9, b"u": 10}.get(kind)
            if count is None:
                raise adapter._malformed()
            fields = record.split(b" ", count)
            if len(fields) != count + 1 or len(fields[1]) != 2:
                raise adapter._malformed()
            xy = fields[1]
            if any(code not in b".MTADRCU" for code in xy):
                raise adapter._malformed()
            original = None
            if kind == b"2":
                if position >= len(records) or not re.fullmatch(
                    rb"[RC](?:100|[0-9]{1,2})", fields[8]
                ):
                    raise adapter._malformed()
                original = adapter._path(records[position])
                position += 1
            changes.append(
                FileChange(
                    adapter._path(fields[-1]),
                    original,
                    chr(xy[0]),
                    chr(xy[1]),
                    conflicted=kind == b"u",
                )
            )
        if len(changes) > adapter.MAX_CHANGES:
            raise ProjectError(
                "git_output_limit", "Git returned too many changes; no partial answer."
            )
    head, oid = headers.get(b"branch.head"), headers.get(b"branch.oid")
    if not head or not oid:
        raise adapter._malformed()
    unborn = oid == b"(initial)"
    detached = head == b"(detached)"
    if (unborn and detached) or (
        not unborn and not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid)
    ):
        raise adapter._malformed()
    branch = BranchState(
        name=None if detached else os.fsdecode(head),
        commit=None if unborn else oid.decode("ascii"),
        detached=detached,
        unborn=unborn,
    )
    return branch, tuple(changes)
