"""Bounded literal copying, sealing and scoped readiness-marker withdrawal."""

import math
import os
import time
from contextlib import ExitStack
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from uuid import uuid4

from jeff.application.execution import (
    PreparedFile,
    PreparedInputBinding,
    PreparedInputFault,
    PreparedInputs,
)
from jeff.domain.execution import PREPARED_TOTAL_BYTES
from jeff.domain.projects import Project

from .filesystem import _FLAGS, _deadline, _identity, _signature
from .metadata import _SEAL_BYTES, _Publication, _Seal

if TYPE_CHECKING:
    from .store import FilePreparedInputStore


def prepare(
    self: "FilePreparedInputStore", project: Project, paths: tuple[str, ...], *, deadline: float
) -> PreparedInputs:
    publication: list[_Publication] = []  # At most one, kept outside resource teardown.
    try:
        return self._prepare(project, paths, deadline=deadline, publication=publication)
    except Exception as error:
        if publication:
            try:
                self._withdraw_published(publication[0])
            except Exception:
                self._failed = True
                raise PreparedInputFault("input_unconfirmed") from None
        if isinstance(error, PreparedInputFault):
            raise
        if isinstance(error, ValueError):
            raise PreparedInputFault("input_invalid") from None
        raise PreparedInputFault("input_unconfirmed") from None


def _prepare(
    self: "FilePreparedInputStore",
    project: Project,
    paths: tuple[str, ...],
    *,
    deadline: float,
    publication: list[_Publication],
) -> PreparedInputs:
    deadline = min(deadline, time.monotonic() + 30) if math.isfinite(deadline) else deadline
    snapshot: int | None = None
    seal_fd: int | None = None
    with ExitStack() as retained:
        postchecks = ExitStack()
        try:
            _deadline(deadline)
            names = self._literal_paths(paths)
            identifier = uuid4()
            mount = self.data / "jobs" / str(identifier) / "input"
            PreparedInputBinding(snapshot_id=identifier, inventory_digest="0" * 64, path=mount)
            with self._base(
                create=True, exclusive=True, deadline=deadline, retained=retained
            ) as jobs:
                self._capacity(jobs, deadline)
                with self._source(project.root, deadline) as source:
                    signatures: dict[str, tuple[int, ...]] = {}
                    identities: set[tuple[int, int]] = set()
                    total = 0
                    for name in names:
                        candidate = project.root / name
                        if any(
                            candidate.is_relative_to(p)
                            for p in (self.data, self.owner.store.layout.runtime_dir)
                        ):
                            raise PreparedInputFault("input_unsafe")
                        with self._file(source, name, deadline, mode=None) as fd:
                            info = os.fstat(fd)
                            key = (info.st_dev, info.st_ino)
                            if key in identities:
                                raise PreparedInputFault("input_unsafe")
                            identities.add(key)
                            signatures[name] = _signature(info)
                            total += info.st_size
                            if total > PREPARED_TOTAL_BYTES:
                                raise PreparedInputFault("input_limit")
                    self._make_directory(jobs, str(identifier))  # Exclusive UUID; no adoption.
                    snapshot = self._directory(
                        jobs, str(identifier), 0o700, retained, postchecks=postchecks
                    )
                    self._make_directory(snapshot, "input")
                    view = self._directory(snapshot, "input", None, retained, postchecks=postchecks)
                    files: list[PreparedFile] = []
                    directories: set[tuple[str, ...]] = {()}
                    for name in names:
                        with (
                            self._file(source, name, deadline, mode=None) as src,
                            ExitStack() as stack,
                        ):
                            if _signature(os.fstat(src)) != signatures[name]:
                                raise PreparedInputFault("input_changed")
                            parts = PurePosixPath(name).parts
                            parent = self._tree(
                                view, parts[:-1], stack, deadline, mode=0o700, create=True
                            )
                            dst = os.open(
                                parts[-1],
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS,
                                0o600,
                                dir_fd=parent,
                            )
                            stack.callback(self._close, dst)
                            size, digest = self._hash(src, deadline, destination=dst)
                            if size != signatures[name][5]:
                                raise PreparedInputFault("input_changed")
                            os.fchmod(dst, 0o444)
                            os.fsync(dst)
                            files.append(PreparedFile(path=name, size=size, sha256=digest))
                            directories.update(parts[:i] for i in range(1, len(parts)))
                    for name in names:
                        with self._file(source, name, deadline, mode=None) as fd:
                            if _signature(os.fstat(fd)) != signatures[name]:
                                raise PreparedInputFault("input_changed")
                for parts in sorted(directories, key=len, reverse=True):
                    with ExitStack() as stack:
                        directory = self._tree(view, parts, stack, deadline, mode=None)
                        os.fchmod(directory, 0o555)
                        os.fsync(directory)
                inventory = PreparedInputs(snapshot_id=identifier, files=tuple(files))
                self._verify_tree(view, inventory, deadline)
                seal = _Seal(
                    project_id=project.project_id,
                    canonical_root=project.root,
                    inventory=inventory,
                    inventory_digest=inventory.digest,
                    input_identity=_identity(os.fstat(view)),
                )
                payload = seal.model_dump_json().encode("utf-8")
                if len(payload) > _SEAL_BYTES:
                    raise PreparedInputFault("input_limit")
                seal_fd = os.open(
                    ".prepared.tmp",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS,
                    0o600,
                    dir_fd=snapshot,
                )
                retained.callback(self._close, seal_fd)
                self._write(seal_fd, payload, deadline)
                os.fsync(seal_fd)
                self.owner.check()
                _deadline(deadline)
                publication.append(
                    (identifier, _identity(os.fstat(snapshot)), _identity(os.fstat(seal_fd)))
                )
                os.link(
                    ".prepared.tmp",
                    "prepared.json",
                    src_dir_fd=snapshot,
                    dst_dir_fd=snapshot,
                    follow_symlinks=False,
                )
                os.unlink(".prepared.tmp", dir_fd=snapshot)
                os.fsync(snapshot)
                os.fsync(jobs)
            # Validate before closing the seal/snapshot FDs or releasing the lock.
            # ExitStack teardown used to run these after leaving this handler.
            postchecks.close()
            return inventory
        except Exception as error:
            try:
                postchecks.close()
            except Exception:
                pass  # Preserve the original failure; all readiness must be withdrawn.
            # Withdraw only our own readiness marker, never remove snapshot contents.
            if snapshot is not None and seal_fd is not None:
                try:
                    entry = os.stat("prepared.json", dir_fd=snapshot, follow_symlinks=False)
                    if _identity(entry) != _identity(os.fstat(seal_fd)):
                        raise OSError
                    os.unlink("prepared.json", dir_fd=snapshot)
                    os.fsync(snapshot)
                except FileNotFoundError:
                    pass
                except OSError:
                    self._failed = True
            publication.clear()  # Withdrawal handled while live FDs/exclusion remain held.
            if self._failed:
                raise PreparedInputFault("input_unconfirmed") from None
            if isinstance(error, PreparedInputFault):
                raise
            if isinstance(error, ValueError):
                raise PreparedInputFault("input_invalid") from None
            raise PreparedInputFault("input_unconfirmed") from None
