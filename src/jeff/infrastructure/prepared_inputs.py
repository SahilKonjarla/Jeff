"""Explicit public-file snapshots; never a live project mount or automatic source recopy.

Mode bits/private ancestry are not a hostile-same-UID or complete ACL proof.
Deadlines are cooperative between filesystem operations, not hard I/O cancellation.
"""

import fcntl
import hashlib
import json
import math
import os
import stat
import time
import unicodedata
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from jeff.application.execution import (
    ExecutionManifest,
    PreparedFile,
    PreparedInputBinding,
    PreparedInputFault,
    PreparedInputs,
    absolute_path,
    canonical_prepared_paths,
)
from jeff.application.runtime import ControlFault
from jeff.domain.execution import PREPARED_FILE_BYTES, PREPARED_TOTAL_BYTES
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import Ownership

_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR = os.O_RDONLY | os.O_DIRECTORY | _FLAGS
_SLOTS = 17
_SEAL_BYTES = 65536
_Publication = tuple[UUID, tuple[int, int, int, int], tuple[int, int, int, int]]


class _Seal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    project_id: UUID
    canonical_root: Path
    inventory: PreparedInputs
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_identity: tuple[StrictInt, StrictInt, StrictInt, StrictInt]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate prepared metadata key.")
        result[key] = value
    return result


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _deadline(deadline: float) -> None:
    if not math.isfinite(deadline) or time.monotonic() >= deadline:
        raise PreparedInputFault("input_timeout")


def _names(fd: int, expected: set[str], deadline: float) -> None:
    # Only inspect expected snapshot directories, never recursively discover source files.
    normalized = {unicodedata.normalize("NFC", name) for name in expected}
    seen: set[str] = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            _deadline(deadline)
            name = unicodedata.normalize("NFC", entry.name)
            if name not in normalized or name in seen:
                raise PreparedInputFault("input_unsafe")
            seen.add(name)
    if seen != normalized:
        raise PreparedInputFault("input_changed")


class FilePreparedInputStore:
    """Installation-owned preparer/verifier; caller supplies a registration-resolved Project.

    A Project value alone is not registration proof. There is no public request or fallback
    constructor, content-based secret certification, or inherited credential access.
    """

    def __init__(self, owner: Ownership) -> None:
        self.owner = owner
        self.uid = owner.store.layout.uid
        self.data = owner.store.layout.data_dir
        self._failed = False

    def _entry_stable(
        self, parent: int, name: str, identity: tuple[int, int, int, int], mode: int | None
    ) -> None:
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current.st_mode)
                or _identity(current)[:2] != identity[:2]
                or current.st_uid != self.uid
                or (mode is not None and stat.S_IMODE(current.st_mode) != mode)
                or (mode is None and current.st_mode & 0o022)
            ):
                raise PreparedInputFault("input_changed")
        except OSError:
            raise PreparedInputFault("input_changed") from None

    def _close(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            self._failed = True
            raise PreparedInputFault("input_unconfirmed") from None

    def _directory(
        self,
        parent: int,
        name: str,
        mode: int | None,
        stack: ExitStack,
        *,
        postchecks: ExitStack | None = None,
    ) -> int:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != self.uid
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
            or (mode is None and stat.S_IMODE(before.st_mode) & 0o022)
        ):
            raise PreparedInputFault("input_unsafe")
        fd = os.open(name, _DIR, dir_fd=parent)
        stack.callback(self._close, fd)
        if _identity(os.fstat(fd)) != _identity(before):
            raise PreparedInputFault("input_changed")
        pinned_parent = os.dup(parent)
        stack.callback(self._close, pinned_parent)
        checks = stack if postchecks is None else postchecks
        checks.callback(self._entry_stable, pinned_parent, name, _identity(before), mode)
        return fd

    def _make_directory(self, parent: int, name: str, *, exist_ok: bool = False) -> None:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            if not exist_ok:
                raise
            return  # Existing entries are strictly checked, never permission-repaired.
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != self.uid
            or stat.S_IMODE(before.st_mode) & ~0o700
        ):
            raise PreparedInputFault("input_unsafe")
        fd = os.open(name, _DIR, dir_fd=parent)
        try:
            if _identity(os.fstat(fd)) != _identity(before):
                raise PreparedInputFault("input_changed")
            # Daemon umask0177 intentionally protects sockets/files but removes
            # owner traversal from mkdir0700. Set only our freshly created inode.
            os.fchmod(fd, 0o700)
            after = os.fstat(fd)
            if (
                after.st_uid != self.uid
                or stat.S_IMODE(after.st_mode) != 0o700
                or _identity(after)
                != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
            ):
                raise PreparedInputFault("input_changed")
        finally:
            self._close(fd)

    def _tree(
        self,
        root: int,
        parts: tuple[str, ...],
        stack: ExitStack,
        deadline: float,
        *,
        mode: int | None,
        create: bool = False,
    ) -> int:
        fd = root
        for name in parts:
            _deadline(deadline)
            if create:
                self._make_directory(fd, name, exist_ok=True)
            fd = self._directory(fd, name, mode, stack)
        return fd

    @contextmanager
    def _source(self, path: Path, deadline: float) -> Iterator[int]:
        absolute_path(path)
        layout = self.owner.store.layout
        if path.is_relative_to(layout.data_dir) or path.is_relative_to(layout.runtime_dir):
            raise PreparedInputFault("input_unsafe")
        with ExitStack() as stack:
            fd = os.open("/", _DIR)
            stack.callback(self._close, fd)
            chain: list[tuple[int, str, tuple[int, int, int, int]]] = []
            for part in path.parts[1:]:
                _deadline(deadline)
                before = os.stat(part, dir_fd=fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(before.st_mode)
                    or before.st_uid not in (0, self.uid)
                    or (before.st_mode & 0o022 and not before.st_mode & stat.S_ISVTX)
                ):
                    raise PreparedInputFault("input_unsafe")
                next_fd = os.open(part, _DIR, dir_fd=fd)
                stack.callback(self._close, next_fd)
                if _identity(before) != _identity(os.fstat(next_fd)):
                    raise PreparedInputFault("input_changed")
                chain.append((fd, part, _identity(before)))
                fd = next_fd
            if os.fstat(fd).st_uid != self.uid:
                raise PreparedInputFault("input_unsafe")
            yield fd
            for parent, name, identity in chain:
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity:
                    raise PreparedInputFault("input_changed")

    @contextmanager
    def _base(
        self, *, create: bool, exclusive: bool, deadline: float, retained: ExitStack
    ) -> Iterator[int]:
        _deadline(deadline)
        if self._failed:
            raise PreparedInputFault("input_unconfirmed")
        self.owner.check()
        with self.owner.store.directory(self.data, create=False) as data, ExitStack() as stack:
            if create:
                self._make_directory(data, "jobs", exist_ok=True)
            jobs = self._directory(data, "jobs", 0o700, stack)
            flags = os.O_RDWR | _FLAGS
            try:
                lock = os.open(".prepare.lock", flags, dir_fd=jobs)
            except FileNotFoundError:
                if not create:
                    raise
                # Match the runtime anchor pattern. Concurrent O_CREAT opens can
                # transiently report ENOENT on macOS; use exclusive publication.
                try:
                    lock = os.open(
                        ".prepare.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=jobs
                    )
                except FileExistsError:
                    lock = os.open(".prepare.lock", flags, dir_fd=jobs)
            retained.callback(
                self._close, lock
            )  # Keep exclusion through postchecks/failed-seal withdrawal.
            before = os.fstat(lock)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != self.uid
                or before.st_nlink != 1
                or before.st_size != 0
                or stat.S_IMODE(before.st_mode) != 0o600
                or _identity(before)
                != _identity(os.stat(".prepare.lock", dir_fd=jobs, follow_symlinks=False))
            ):
                raise PreparedInputFault("input_unsafe")
            try:
                fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PreparedInputFault("input_busy") from None
            yield jobs
            self.owner.check()
            if _identity(before) != _identity(
                os.stat(".prepare.lock", dir_fd=jobs, follow_symlinks=False)
            ):
                raise PreparedInputFault("input_changed")

    def _capacity(self, jobs: int, deadline: float) -> None:
        count = 0
        with os.scandir(jobs) as entries:
            for entry in entries:
                _deadline(deadline)
                if entry.name == ".prepare.lock":
                    continue
                try:
                    if str(UUID(entry.name)) != entry.name:
                        raise ValueError
                except ValueError:
                    raise PreparedInputFault("input_unsafe") from None
                if not entry.is_dir(follow_symlinks=False):
                    raise PreparedInputFault("input_unsafe")
                count += 1
                if count >= _SLOTS:
                    raise PreparedInputFault("input_capacity")

    def _literal_paths(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        return canonical_prepared_paths(paths)

    def _regular(self, info: os.stat_result, mode: int | None, limit: int) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != self.uid
            or info.st_nlink != 1
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
            or (mode is None and info.st_mode & 0o022)
        ):
            raise PreparedInputFault("input_unsafe")
        if info.st_size > limit:
            raise PreparedInputFault("input_limit")

    @contextmanager
    def _file(
        self,
        root: int,
        name: str,
        deadline: float,
        *,
        mode: int | None,
        limit: int = PREPARED_FILE_BYTES,
        directory_mode: int | None = None,
    ) -> Iterator[int]:
        with ExitStack() as stack:
            parts = PurePosixPath(name).parts
            directory = self._tree(root, parts[:-1], stack, deadline, mode=directory_mode)
            before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            self._regular(before, mode, limit)
            fd = os.open(parts[-1], os.O_RDONLY | _FLAGS, dir_fd=directory)
            stack.callback(self._close, fd)
            if _signature(before) != _signature(os.fstat(fd)):
                raise PreparedInputFault("input_changed")
            yield fd
            if _signature(before) != _signature(os.fstat(fd)) or _signature(before) != _signature(
                os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            ):
                raise PreparedInputFault("input_changed")

    @staticmethod
    def _write(fd: int, data: bytes, deadline: float) -> None:
        view = memoryview(data)
        while view:
            _deadline(deadline)
            count = os.write(fd, view)
            if count <= 0:
                raise OSError
            view = view[count:]

    def _hash(self, fd: int, deadline: float, *, destination: int | None = None) -> tuple[int, str]:
        total = 0
        digest = hashlib.sha256()
        while True:
            _deadline(deadline)
            block = os.read(fd, 65536)
            if not block:
                break
            total += len(block)
            if total > PREPARED_FILE_BYTES:
                raise PreparedInputFault("input_limit")
            digest.update(block)
            if destination is not None:
                self._write(destination, block, deadline)
        return total, digest.hexdigest()

    def _verify_tree(self, root: int, inventory: PreparedInputs, deadline: float) -> None:
        expected: dict[tuple[str, ...], set[str]] = {(): set()}
        for item in inventory.files:
            parts = PurePosixPath(item.path).parts
            for index, part in enumerate(parts):
                expected.setdefault(parts[:index], set()).add(part)
        for parts, names in expected.items():
            with ExitStack() as stack:
                fd = self._tree(root, parts, stack, deadline, mode=0o555)
                _names(fd, names, deadline)
        for item in inventory.files:
            with self._file(root, item.path, deadline, mode=0o444, directory_mode=0o555) as fd:
                if self._hash(fd, deadline) != (item.size, item.sha256):
                    raise PreparedInputFault("input_changed")

    def _withdraw_published(self, publication: _Publication) -> None:
        """Close acknowledgement may be lost: reopen only our captured UUID/inodes.

        This withdraws readiness, never snapshot contents or an unknown replacement.
        """
        identifier, snapshot_identity, seal_identity = publication
        self.owner.check()
        with self.owner.store.directory(self.data, create=False) as data, ExitStack() as stack:
            jobs = self._directory(data, "jobs", 0o700, stack)
            snapshot = self._directory(jobs, str(identifier), 0o700, stack)
            if _identity(os.fstat(snapshot)) != snapshot_identity:
                raise PreparedInputFault("input_unconfirmed")
            try:
                entry = os.stat("prepared.json", dir_fd=snapshot, follow_symlinks=False)
            except FileNotFoundError:
                return
            if _identity(entry) != seal_identity:
                raise PreparedInputFault("input_unconfirmed")
            os.unlink("prepared.json", dir_fd=snapshot)
            os.fsync(snapshot)

    def prepare(
        self, project: Project, paths: tuple[str, ...], *, deadline: float
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
        self,
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
                        view = self._directory(
                            snapshot, "input", None, retained, postchecks=postchecks
                        )
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

    def verify(self, manifest: ExecutionManifest, *, deadline: float) -> PreparedInputBinding:
        try:
            if not isinstance(manifest.inputs.snapshot_id, UUID):
                raise PreparedInputFault("input_invalid")
            self._literal_paths(tuple(item.path for item in manifest.inputs.files))
            with ExitStack() as retained:
                with self._base(
                    create=False, exclusive=False, deadline=deadline, retained=retained
                ) as jobs:
                    snapshot = self._directory(
                        jobs, str(manifest.inputs.snapshot_id), 0o700, retained
                    )
                    _names(snapshot, {"input", "prepared.json"}, deadline)
                    with self._file(
                        snapshot, "prepared.json", deadline, mode=0o600, limit=_SEAL_BYTES
                    ) as fd:
                        payload = bytearray()
                        while True:
                            _deadline(deadline)
                            block = os.read(fd, min(65536, _SEAL_BYTES + 1 - len(payload)))
                            if not block:
                                break
                            payload.extend(block)
                            if len(payload) > _SEAL_BYTES:
                                raise PreparedInputFault("input_limit")
                    seal = _Seal.model_validate(
                        json.loads(payload, object_pairs_hook=_unique_object)
                    )
                    if (
                        seal.project_id != manifest.profile.project_id
                        or seal.canonical_root != manifest.profile.canonical_root
                        or seal.inventory != manifest.inputs
                        or seal.inventory_digest != manifest.inputs.digest
                    ):
                        raise PreparedInputFault("input_scope_mismatch")
                    view = self._directory(snapshot, "input", 0o555, retained)
                    if _identity(os.fstat(view)) != seal.input_identity:
                        raise PreparedInputFault("input_changed")
                    self._verify_tree(view, manifest.inputs, deadline)
                    return PreparedInputBinding(
                        snapshot_id=manifest.inputs.snapshot_id,
                        inventory_digest=manifest.inputs.digest,
                        path=self.data / "jobs" / str(manifest.inputs.snapshot_id) / "input",
                    )
        except PreparedInputFault:
            raise
        except FileNotFoundError:
            raise PreparedInputFault("input_unsealed") from None
        except (OSError, ControlFault, ValueError):
            raise PreparedInputFault("input_unconfirmed") from None

    def check(
        self, binding: PreparedInputBinding, manifest: ExecutionManifest, *, deadline: float
    ) -> None:
        if not isinstance(manifest.inputs.snapshot_id, UUID):
            raise PreparedInputFault("input_invalid")
        try:
            expected = PreparedInputBinding(
                snapshot_id=manifest.inputs.snapshot_id,
                inventory_digest=manifest.inputs.digest,
                path=self.data / "jobs" / str(manifest.inputs.snapshot_id) / "input",
            )
        except ValueError:
            raise PreparedInputFault("input_invalid") from None
        if binding != expected:
            raise PreparedInputFault("input_scope_mismatch")
        if self.verify(manifest, deadline=deadline) != binding:
            raise PreparedInputFault("input_changed")
