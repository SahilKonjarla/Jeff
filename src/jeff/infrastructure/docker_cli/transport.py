"""Pinned private CLI/config/HOME resources and captured bounded metadata transport."""

import hashlib
import math
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from jeff.application.compose_status import COMPOSE_LOG_LIMIT
from jeff.application.execution import DockerBackendFault, DockerCLIIdentity, ExecutionStopSignal

from .diagnostics import _failure_facts
from .queries import _ENGINE_FORMAT, _IMAGE_FORMAT, _LIMIT, Query


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _construction_budget(deadline: float | None) -> None:
    if deadline is not None and (not math.isfinite(deadline) or time.monotonic() >= deadline):
        raise DockerBackendFault("docker_timeout")


def _safe_ancestors(path: Path, *, deadline: float | None = None) -> None:
    if not path.is_absolute():
        raise DockerBackendFault("docker_unsafe_configuration")
    for parent in (*reversed(path.parents), path):
        _construction_budget(deadline)
        info = os.lstat(parent)
        sticky_temp = (
            parent in (Path("/private/tmp"), Path("/private/var/tmp"))
            and info.st_uid == 0
            and bool(info.st_mode & stat.S_ISVTX)
        )
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in (0, os.geteuid())
            or (info.st_mode & 0o022 and not sticky_temp)
        ):
            raise DockerBackendFault("docker_unsafe_configuration")


class _DockerCLI:
    def __init__(
        self, binary: Path, config: Path, home: Path, *, deadline: float | None = None
    ) -> None:
        if sys.platform != "darwin":
            raise DockerBackendFault("docker_unsupported")
        try:
            _construction_budget(deadline)
            self.binary = binary.resolve(strict=True)
            self.config = config
            self.home = home
            if home == config or home in config.parents or config in home.parents:
                raise DockerBackendFault("docker_unsafe_configuration")
            self.identity, self._binary_signature = self._binary_identity(deadline=deadline)
            self._home_signature = self._private_home(fresh=True, deadline=deadline)
            self._config_signature = self._private_config(deadline=deadline)
            _construction_budget(deadline)
        except OSError:
            raise DockerBackendFault("docker_unavailable") from None

    def _binary_identity(
        self, *, deadline: float | None = None
    ) -> tuple[DockerCLIIdentity, tuple[int, ...]]:
        _safe_ancestors(self.binary.parent, deadline=deadline)
        fd = os.open(self.binary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            if not (
                stat.S_ISREG(before.st_mode)
                and before.st_uid in (0, os.geteuid())
                and before.st_nlink == 1
                and not before.st_mode & 0o022
                and before.st_mode & 0o111
                and 0 < before.st_size <= 268435456
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
            digest, count = hashlib.sha256(), 0
            while chunk := os.read(fd, 1048576):
                _construction_budget(deadline)
                count += len(chunk)
                if count > 268435456:
                    raise DockerBackendFault("docker_unsafe_configuration")
                digest.update(chunk)
            if count != before.st_size or _signature(before) != _signature(os.fstat(fd)):
                raise DockerBackendFault("docker_unsafe_configuration")
            return DockerCLIIdentity(
                path=self.binary, sha256=digest.hexdigest(), size=count
            ), _signature(before)
        finally:
            os.close(fd)

    def _private_config(
        self, *, deadline: float | None = None
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if not self.config.is_absolute() or self.config.resolve(strict=True) != self.config:
            raise DockerBackendFault("docker_unsafe_configuration")
        _safe_ancestors(self.config, deadline=deadline)
        directory = os.lstat(self.config)
        if not (
            stat.S_ISDIR(directory.st_mode)
            and directory.st_uid == os.geteuid()
            and stat.S_IMODE(directory.st_mode) == 0o700
        ):
            raise DockerBackendFault("docker_unsafe_configuration")
        # Inspect names only. Never open arbitrary credential/context/plugin entries.
        with os.scandir(self.config) as entries:
            names = [entry.name for _, entry in zip(range(2), entries, strict=False)]
        if names != ["config.json"]:
            raise DockerBackendFault("docker_unsafe_configuration")
        fd = os.open(self.config / "config.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            if not (
                stat.S_ISREG(before.st_mode)
                and before.st_uid == os.geteuid()
                and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o600
                and before.st_size in (2, 3)
                and os.read(fd, 4) in (b"{}", b"{}\n")
                and _signature(before) == _signature(os.fstat(fd))
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
            return _signature(directory), _signature(before)
        finally:
            os.close(fd)

    def _private_home(
        self, *, fresh: bool = False, deadline: float | None = None
    ) -> tuple[int, ...]:
        if not self.home.is_absolute() or self.home.resolve(strict=True) != self.home:
            raise DockerBackendFault("docker_unsafe_configuration")
        _safe_ancestors(self.home, deadline=deadline)
        info = os.lstat(self.home)
        if not (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700
        ):
            raise DockerBackendFault("docker_unsafe_configuration")
        if fresh:
            with os.scandir(self.home) as entries:
                if next(entries, None) is not None:
                    raise DockerBackendFault("docker_unsafe_configuration")
        # The trusted CLI may create caches here. Pin identity/permissions, not cache metadata.
        return info.st_dev, info.st_ino, info.st_uid, info.st_mode

    def check(self) -> None:
        try:
            identity, signature = self._binary_identity()
            if (
                identity != self.identity
                or signature != self._binary_signature
                or self._private_config() != self._config_signature
                or self._private_home() != self._home_signature
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
        except OSError:
            raise DockerBackendFault("docker_unsafe_configuration") from None

    @staticmethod
    def _socket(endpoint: str) -> tuple[int, ...]:
        if not endpoint.startswith("unix:///"):
            raise DockerBackendFault("docker_unsafe_configuration")
        path = Path(endpoint[7:])
        try:
            _safe_ancestors(path.parent)
            info = os.lstat(path)
            if not (
                path.is_absolute()
                and ".." not in path.parts
                and path.resolve(strict=True) == path
                and stat.S_ISSOCK(info.st_mode)
                and info.st_uid == os.geteuid()
                and info.st_nlink == 1
                and not info.st_mode & 0o022
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
            # Socket size/timestamps are not transport identity or authentication.
            return info.st_dev, info.st_ino, info.st_uid, info.st_mode
        except OSError:
            raise DockerBackendFault("docker_unavailable") from None

    def read(
        self,
        query: Query,
        *,
        endpoint: str,
        image_id: str | None,
        deadline: float,
        stop: ExecutionStopSignal | None = None,
    ) -> bytes:
        args: tuple[str, ...]
        if query == "client.version" and image_id is None:
            args = ("version", "--format", "{{json .Client.Version}}")
        elif query == "engine.info" and image_id is None:
            args = ("info", "--format", _ENGINE_FORMAT)
        elif (
            query == "image.inspect"
            and image_id is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        ):
            args = ("image", "inspect", "--format", _IMAGE_FORMAT, "--", image_id)
        else:
            raise DockerBackendFault("docker_unsafe_configuration")
        return self._control(args, endpoint=endpoint, deadline=deadline, stop=stop)

    def _environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "DOCKER_CONFIG": str(self.config),
            "TMPDIR": str(self.home),
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "NO_COLOR": "1",
        }

    def _control(
        self,
        args: tuple[str, ...],
        *,
        endpoint: str,
        deadline: float,
        observe_reply: Callable[[bytes], None] | None = None,
        stop: ExecutionStopSignal | None = None,
    ) -> bytes:
        self.check()
        socket_identity = self._socket(endpoint)
        result = self._capture(
            [str(self.binary), "--config", str(self.config), "--host", endpoint, *args],
            self._environment(),
            min(deadline, time.monotonic() + 5),
            stop=stop,
        )
        if observe_reply is not None:
            observe_reply(result)  # Capture bounded neutral evidence before post-operation checks.
        self.check()
        if self._socket(endpoint) != socket_identity:
            raise DockerBackendFault("docker_target_mismatch")
        if time.monotonic() >= deadline:
            raise DockerBackendFault("docker_timeout")
        return result

    def _capture(
        self,
        argv: list[str],
        environment: dict[str, str],
        deadline: float,
        *,
        stop: ExecutionStopSignal | None = None,
        log_sink: Callable[[Literal["stdout", "stderr"], bytes], None] | None = None,
    ) -> bytes:
        if deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        output, count, stderr_count = bytearray(), 0, 0
        limit = _LIMIT if log_sink is None else COMPOSE_LOG_LIMIT
        diagnostic_prefix = bytearray()  # At most2KiB in RAM, never persisted/reported.
        try:
            if stop is not None:
                stop.check()
            process = subprocess.Popen(
                argv,
                cwd=self.config,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            assert process.stdout is not None and process.stderr is not None
            for pipe, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, label)
            while selector.get_map() or process.poll() is None:
                if stop is not None:
                    stop.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerBackendFault("docker_timeout")
                for key, _ in selector.select(min(0.05, remaining)):
                    try:
                        chunk = os.read(key.fd, 16384)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    count += len(chunk)
                    if count > limit:
                        raise DockerBackendFault("docker_output_limit")
                    if log_sink is not None:
                        log_sink("stdout" if key.data == "stdout" else "stderr", chunk)
                    elif key.data == "stdout":
                        output.extend(chunk)
                    else:
                        stderr_count += len(chunk)
                        diagnostic_prefix.extend(chunk[: max(0, 2048 - len(diagnostic_prefix))])
            code = process.wait(timeout=1)
            if code != 0 or stderr_count:
                raise DockerBackendFault(
                    "docker_command_failed",
                    diagnostics=_failure_facts(
                        code, len(output), stderr_count, bytes(diagnostic_prefix)
                    ),
                )
            return bytes(output)
        except OSError:
            raise DockerBackendFault("docker_unavailable") from None
        finally:
            selector.close()
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()  # Captured CLI only, never a stored host PID.
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    raise DockerBackendFault("docker_cleanup_unconfirmed") from None
                finally:
                    for remaining_pipe in (process.stdout, process.stderr):
                        if remaining_pipe is not None:
                            remaining_pipe.close()
