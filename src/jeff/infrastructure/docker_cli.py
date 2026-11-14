"""Explicit Docker metadata and fixed lifecycle transport; no public routing or launch policy.

The CLI/config/endpoint are explicit. Metadata is bounded and kept in memory only;
raw stdout/stderr is never persisted or attached to errors. Path/signature checks
narrow replacement races, not hostile same-UID or Docker-controller compromise.
"""

import hashlib
import json
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
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from jeff.application.execution import (
    DockerBackendFault,
    DockerCLIIdentity,
    DockerFailureFacts,
    DockerLaunchIntent,
    DockerOutputSink,
    DockerTargetDeclaration,
    VerifiedDockerTarget,
)
from jeff.domain.execution import fingerprint

Query = Literal["client.version", "engine.info", "image.inspect"]
_LIMIT = 65536
_ENGINE_FORMAT = (
    '{"ID":{{json .ID}},"OSType":{{json .OSType}},"Architecture":{{json .Architecture}},'
    '"OperatingSystem":{{json .OperatingSystem}},"ServerVersion":{{json .ServerVersion}},'
    '"SecurityOptions":{{json .SecurityOptions}},"MemoryLimit":{{json .MemoryLimit}},'
    '"PidsLimit":{{json .PidsLimit}},"CPUCfsPeriod":{{json .CPUCfsPeriod}},'
    '"CPUCfsQuota":{{json .CPUCfsQuota}}}'
)
_IMAGE_FORMAT = (
    '{"ID":{{json .Id}},"OS":{{json .Os}},"Architecture":{{json .Architecture}},'
    '"Variant":{{json .Variant}},"Config":{{json .Config}}}'
)


class _Reader(Protocol):
    identity: DockerCLIIdentity

    def check(self) -> None: ...

    def read(
        self, query: Query, *, endpoint: str, image_id: str | None, deadline: float
    ) -> bytes: ...


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


def _safe_ancestors(path: Path) -> None:
    if not path.is_absolute():
        raise DockerBackendFault("docker_unsafe_configuration")
    for parent in (*reversed(path.parents), path):
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


def _failure_facts(
    code: int, stdout_bytes: int, stderr_bytes: int, prefix: bytes
) -> DockerFailureFacts:
    values: dict[str, object] = {
        "returncode": code,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "category": "stderr_on_success" if code == 0 else "other_cli_failure",
    }
    # Match only fixed template syntax and known schema field names. No raw snippets escape.
    for field in (
        "ID",
        "Id",
        "OS",
        "Os",
        "Architecture",
        "Variant",
        "Config",
        "Env",
        "User",
        "Entrypoint",
        "Cmd",
        "WorkingDir",
        "Volumes",
        "Healthcheck",
        "OnBuild",
    ):
        name = re.escape(field.encode("ascii"))
        if re.search(rb"can't evaluate field " + name + rb"(?:\s|$)", prefix) or re.search(
            rb"map has no entry for key [\"']" + name + rb"[\"']", prefix
        ):
            values.update(category="template_field_unavailable", field_name=field)
            break
    return DockerFailureFacts.model_validate(values)


class _DockerCLI:
    def __init__(self, binary: Path, config: Path, home: Path) -> None:
        if sys.platform != "darwin":
            raise DockerBackendFault("docker_unsupported")
        try:
            self.binary = binary.resolve(strict=True)
            self.config = config
            self.home = home
            if home == config or home in config.parents or config in home.parents:
                raise DockerBackendFault("docker_unsafe_configuration")
            self.identity, self._binary_signature = self._binary_identity()
            self._home_signature = self._private_home(fresh=True)
            self._config_signature = self._private_config()
        except OSError:
            raise DockerBackendFault("docker_unavailable") from None

    def _binary_identity(self) -> tuple[DockerCLIIdentity, tuple[int, ...]]:
        _safe_ancestors(self.binary.parent)
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

    def _private_config(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if not self.config.is_absolute() or self.config.resolve(strict=True) != self.config:
            raise DockerBackendFault("docker_unsafe_configuration")
        _safe_ancestors(self.config)
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

    def _private_home(self, *, fresh: bool = False) -> tuple[int, ...]:
        if not self.home.is_absolute() or self.home.resolve(strict=True) != self.home:
            raise DockerBackendFault("docker_unsafe_configuration")
        _safe_ancestors(self.home)
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

    def read(self, query: Query, *, endpoint: str, image_id: str | None, deadline: float) -> bytes:
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
        return self._control(args, endpoint=endpoint, deadline=deadline)

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
    ) -> bytes:
        self.check()
        socket_identity = self._socket(endpoint)
        result = self._capture(
            [str(self.binary), "--config", str(self.config), "--host", endpoint, *args],
            self._environment(),
            min(deadline, time.monotonic() + 5),
        )
        if observe_reply is not None:
            observe_reply(result)  # Capture bounded neutral evidence before post-operation checks.
        self.check()
        if self._socket(endpoint) != socket_identity:
            raise DockerBackendFault("docker_target_mismatch")
        if time.monotonic() >= deadline:
            raise DockerBackendFault("docker_timeout")
        return result

    def _capture(self, argv: list[str], environment: dict[str, str], deadline: float) -> bytes:
        if deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        output, count, stderr_count = bytearray(), 0, 0
        diagnostic_prefix = bytearray()  # At most2KiB in RAM, never persisted/reported.
        try:
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
                    if count > _LIMIT:
                        raise DockerBackendFault("docker_output_limit")
                    if key.data == "stdout":
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


class DockerLifecycleCLI(_DockerCLI):
    """Fixed internal operations. A controller must supply live ownership/intent proofs.

    Not composed into bootstrap/RPC/CLI until real journal/input/output adapters exist.
    """

    @staticmethod
    def _id(container_id: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        return container_id

    def engine_id(self, endpoint: str, *, deadline: float) -> str:
        return _Engine.model_validate(
            _json(self.read("engine.info", endpoint=endpoint, image_id=None, deadline=deadline))
        ).ID

    def create(self, intent: DockerLaunchIntent, *, deadline: float) -> str:
        from jeff.infrastructure.runtime_execution import create_arguments

        if intent.target.binary != self.identity:
            raise DockerBackendFault("docker_target_mismatch")
        observed: str | None = None

        def capture_id(reply: bytes) -> None:
            nonlocal observed
            try:
                observed = self._id(reply.strip().decode("ascii"))
            except (UnicodeError, ValueError, DockerBackendFault):
                raise DockerBackendFault("docker_mutation_unconfirmed") from None

        try:
            self._control(
                create_arguments(intent),
                endpoint=intent.target.endpoint,
                deadline=deadline,
                observe_reply=capture_id,
            )
        except DockerBackendFault as error:
            raise DockerBackendFault(
                "docker_mutation_unconfirmed",
                diagnostics=error.diagnostics,
                observed_container_id=observed,
            ) from None
        assert observed is not None
        return observed

    def inspect(self, endpoint: str, container_id: str, *, deadline: float) -> dict[str, object]:
        template = (
            '{"Id":{{json .Id}},"Image":{{json .Image}},"Created":{{json .Created}},'
            '"Path":{{json .Path}},"Args":{{json .Args}},"Config":{{json .Config}},'
            '"HostConfig":{{json .HostConfig}},"Mounts":{{json .Mounts}},"State":{{json .State}}}'
        )
        try:
            value = _json(
                self._control(
                    ("container", "inspect", "--format", template, "--", self._id(container_id)),
                    endpoint=endpoint,
                    deadline=deadline,
                )
            )
            if not isinstance(value, dict):
                raise ValueError("Container metadata must be an object.")
            return cast(dict[str, object], value)
        except (ValueError, TypeError, RecursionError):
            raise DockerBackendFault("docker_protocol_error") from None

    def stop(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        # Docker stop may escalate internally after its one-second grace period.
        self._control(
            ("container", "stop", "--timeout", "1", "--", self._id(container_id)),
            endpoint=endpoint,
            deadline=deadline,
        )

    def kill(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        self._control(
            ("container", "kill", "--signal", "KILL", "--", self._id(container_id)),
            endpoint=endpoint,
            deadline=deadline,
        )

    def remove(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        self._control(
            ("container", "rm", "--", self._id(container_id)), endpoint=endpoint, deadline=deadline
        )  # Never force/volume/broad cleanup.

    def absent(self, endpoint: str, container_id: str, *, deadline: float) -> bool:
        identifier = self._id(container_id)
        result = self._control(
            ("container", "ls", "--all", "--quiet", "--no-trunc", "--filter", "id=" + identifier),
            endpoint=endpoint,
            deadline=deadline,
        ).strip()
        if result not in (b"", identifier.encode("ascii")):
            raise DockerBackendFault("docker_protocol_error")
        return not result

    def attach(
        self, endpoint: str, container_id: str, sink: DockerOutputSink, *, deadline: float
    ) -> int:
        self.check()
        socket_identity = self._socket(endpoint)
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")
        argv = [
            str(self.binary),
            "--config",
            str(self.config),
            "--host",
            endpoint,
            "container",
            "start",
            "--attach",
            "--",
            self._id(container_id),
        ]
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.config,
                env=self._environment(),
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
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerBackendFault("docker_timeout")
                for key, _ in selector.select(min(0.05, remaining)):
                    try:
                        chunk = os.read(key.fd, 16384)
                    except BlockingIOError:
                        continue
                    stream: Literal["stdout", "stderr"] = key.data
                    if chunk:
                        sink.accept(stream, chunk)  # Trusted bounded in-memory redactor only.
                    else:
                        selector.unregister(key.fileobj)
                        sink.finish(stream)
            code = process.wait(timeout=1)
            self.check()
            if self._socket(endpoint) != socket_identity:
                raise DockerBackendFault("docker_target_mismatch")
            return code  # CLI status is not the container's authoritative exit observation.
        except DockerBackendFault:
            raise
        except OSError:
            raise DockerBackendFault("docker_unavailable") from None
        except Exception:
            raise DockerBackendFault("docker_output_failed") from None
        finally:
            selector.close()
            if process is not None:
                try:
                    if process.poll() is None:
                        # start --attach may proxy SIGTERM. Kill only the captured CLI;
                        # the controller separately revalidates ownership before job stop/kill.
                        process.kill()
                        process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    raise DockerBackendFault("docker_cleanup_unconfirmed") from None
                finally:
                    for pipe_to_close in (process.stdout, process.stderr):
                        if pipe_to_close is not None:
                            pipe_to_close.close()


class _Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _Engine(_Metadata):
    ID: str = Field(min_length=1, max_length=128)
    OSType: str
    Architecture: str
    OperatingSystem: str
    ServerVersion: str = Field(max_length=64)
    SecurityOptions: tuple[str, ...] = Field(max_length=16)
    MemoryLimit: StrictBool
    PidsLimit: StrictBool
    CPUCfsPeriod: StrictBool
    CPUCfsQuota: StrictBool


class _ImageConfig(_Metadata):
    Env: tuple[str, ...] | None = Field(default=None, max_length=32)
    User: str = Field(default="", max_length=128)
    Entrypoint: tuple[str, ...] | None = Field(default=None, max_length=64)
    Cmd: tuple[str, ...] | None = Field(default=None, max_length=64)
    WorkingDir: str = Field(default="", max_length=1023)
    Volumes: dict[str, object] | None = None
    Healthcheck: dict[str, object] | None = None
    OnBuild: tuple[str, ...] | None = None
    # Raw OCI Config may omit defaults or include known inert metadata. Reject active extras.
    Labels: dict[str, str] | None = Field(default=None, max_length=0)
    ExposedPorts: dict[str, object] | None = Field(default=None, max_length=0)
    ArgsEscaped: StrictBool = False
    StopSignal: Literal[""] | None = None
    Shell: tuple[str, ...] | None = Field(default=None, max_length=0)


class _Image(_Metadata):
    ID: str
    OS: str
    Architecture: str
    Variant: str | None = None
    Config: _ImageConfig


def _json(data: bytes) -> object:
    if not 0 < len(data) <= _LIMIT:
        raise DockerBackendFault("docker_output_limit")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate metadata key.")
            result[key] = value
        return result

    def finite(value: str) -> object:
        raise ValueError("Nonfinite metadata.")

    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=finite)


class DockerTargetInspector:
    """Read-only verifier. No authority-store calls, public routing or lifecycle methods."""

    def __init__(
        self,
        binary: Path | None = None,
        config: Path | None = None,
        *,
        home: Path | None = None,
        _reader: _Reader | None = None,
    ) -> None:
        if _reader is None:
            if binary is None or config is None or home is None:
                raise DockerBackendFault("docker_unsafe_configuration")
            _reader = _DockerCLI(binary, config, home)
        self._reader = _reader

    def verify_target(
        self, declaration: DockerTargetDeclaration, *, deadline: float
    ) -> VerifiedDockerTarget:
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")
        deadline = min(deadline, time.monotonic() + 20)
        profile = declaration.profile
        try:
            self._reader.check()
            version = _json(
                self._reader.read(
                    "client.version", endpoint=profile.endpoint, image_id=None, deadline=deadline
                )
            )
            if not isinstance(version, str) or not re.fullmatch(r"29\.[0-9]+\.[0-9]+", version):
                raise DockerBackendFault("docker_unsupported")
            engine = _Engine.model_validate(
                _json(
                    self._reader.read(
                        "engine.info", endpoint=profile.endpoint, image_id=None, deadline=deadline
                    )
                )
            )
            arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(
                engine.Architecture
            )
            if engine.ID != profile.engine_id:
                raise DockerBackendFault("docker_target_mismatch")
            if (
                engine.OSType != "linux"
                or engine.OperatingSystem != "Docker Desktop"
                or profile.platform != f"linux/{arch}"
                or not re.fullmatch(r"29\.[0-9]+\.[0-9]+", engine.ServerVersion)
                or not all(
                    (engine.MemoryLimit, engine.PidsLimit, engine.CPUCfsPeriod, engine.CPUCfsQuota)
                )
                or "name=seccomp,profile=builtin" not in engine.SecurityOptions
            ):
                raise DockerBackendFault("docker_unsupported")
            image = _Image.model_validate(
                _json(
                    self._reader.read(
                        "image.inspect",
                        endpoint=profile.endpoint,
                        image_id=profile.image_id,
                        deadline=deadline,
                    )
                )
            )
            if (
                image.ID != profile.image_id
                or f"{image.OS}/{image.Architecture}" != profile.platform
            ):
                raise DockerBackendFault("docker_target_mismatch")
            if image.Variant not in (None, "", "v8") or (
                image.Architecture != "arm64" and image.Variant not in (None, "")
            ):
                raise DockerBackendFault("docker_unsupported")
            config = image.Config
            if config.Volumes or config.Healthcheck or config.OnBuild or config.ArgsEscaped:
                raise DockerBackendFault("docker_unsupported")
            environment: dict[str, str] = {}
            for item in config.Env or ():
                key, separator, value = item.partition("=")
                if not separator or key in environment:
                    raise DockerBackendFault("docker_protocol_error")
                environment[key] = value
            if (
                tuple(sorted(environment.items())) != declaration.image_environment
                or config.User != declaration.image_user
                or (config.Entrypoint or ()) != declaration.image_entrypoint
                or (config.Cmd or ()) != declaration.image_command
                or config.WorkingDir != declaration.image_working_dir
            ):
                raise DockerBackendFault("docker_target_mismatch")
            final_engine = _Engine.model_validate(
                _json(
                    self._reader.read(
                        "engine.info", endpoint=profile.endpoint, image_id=None, deadline=deadline
                    )
                )
            )
            if final_engine != engine:
                raise DockerBackendFault("docker_target_mismatch")
            self._reader.check()
            if time.monotonic() >= deadline:
                raise DockerBackendFault("docker_timeout")
            environment.update(profile.public_environment)
            return VerifiedDockerTarget(
                declaration_digest=declaration.fingerprint,
                profile_digest=profile.fingerprint,
                binary=self._reader.identity,
                endpoint=profile.endpoint,
                engine_id=engine.ID,
                image_id=image.ID,
                platform=profile.platform,
                client_version=version,
                server_version=engine.ServerVersion,
                image_configuration_digest=fingerprint(config.model_dump()),
                effective_environment=tuple(sorted(environment.items())),
            )
        except OSError:
            raise DockerBackendFault("docker_unavailable") from None
        except (ValueError, RecursionError, UnicodeError, TypeError):
            raise DockerBackendFault("docker_protocol_error") from None
