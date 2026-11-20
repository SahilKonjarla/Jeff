"""Installation-owned descriptor lifecycle, namespace checks and workflow delegates."""

import fcntl
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from uuid import UUID

from jeff.application.execution import (
    ExecutionManifest,
    PreparedInputBinding,
    PreparedInputFault,
    PreparedInputs,
    absolute_path,
    canonical_prepared_paths,
)
from jeff.domain.execution import PREPARED_FILE_BYTES
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import Ownership

from . import preparation, verification
from .filesystem import _DIR, _FLAGS, _SLOTS, _deadline, _identity, _names, _signature
from .metadata import _Publication


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
        return preparation.prepare(self, project, paths, deadline=deadline)

    def _prepare(
        self,
        project: Project,
        paths: tuple[str, ...],
        *,
        deadline: float,
        publication: list[_Publication],
    ) -> PreparedInputs:
        return preparation._prepare(
            self, project, paths, deadline=deadline, publication=publication
        )

    def verify(self, manifest: ExecutionManifest, *, deadline: float) -> PreparedInputBinding:
        return verification.verify(self, manifest, deadline=deadline)

    def check(
        self, binding: PreparedInputBinding, manifest: ExecutionManifest, *, deadline: float
    ) -> None:
        return verification.check(self, binding, manifest, deadline=deadline)
