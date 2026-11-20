"""Read-only declared target verification, separate from launch authority."""

import math
import re
import time
from pathlib import Path

from jeff.application.execution import (
    DockerBackendFault,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    ExecutionStopSignal,
    VerifiedDockerTarget,
)
from jeff.domain.execution import fingerprint

from .metadata import _Engine, _Image, _json
from .queries import Query, _Reader
from .transport import _DockerCLI


class DockerTargetInspector:
    """Read-only verifier. No authority-store calls, public routing or lifecycle methods."""

    def __init__(
        self,
        binary: Path | None = None,
        config: Path | None = None,
        *,
        home: Path | None = None,
        _reader: _Reader | None = None,
        deadline: float | None = None,
    ) -> None:
        if _reader is None:
            if binary is None or config is None or home is None:
                raise DockerBackendFault("docker_unsafe_configuration")
            _reader = _DockerCLI(binary, config, home, deadline=deadline)
        self._reader = _reader

    @property
    def identity(self) -> DockerCLIIdentity:
        return self._reader.identity

    def verify_target(
        self, declaration: DockerTargetDeclaration, *, deadline: float
    ) -> VerifiedDockerTarget:
        return self._verify_target(declaration, deadline=deadline)

    def verify_target_cancellable(
        self, declaration: DockerTargetDeclaration, stop: ExecutionStopSignal, *, deadline: float
    ) -> VerifiedDockerTarget:
        if stop.authority.manifest.profile != declaration.profile:
            raise DockerBackendFault("docker_authority_required")
        return self._verify_target(declaration, deadline=deadline, stop=stop)

    def _verify_target(
        self,
        declaration: DockerTargetDeclaration,
        *,
        deadline: float,
        stop: ExecutionStopSignal | None = None,
    ) -> VerifiedDockerTarget:
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")
        deadline = min(deadline, time.monotonic() + 20)
        profile = declaration.profile

        def read(query: Query, *, endpoint: str, image_id: str | None, deadline: float) -> bytes:
            if stop is None:
                return self._reader.read(
                    query, endpoint=endpoint, image_id=image_id, deadline=deadline
                )
            stop.check()
            if isinstance(self._reader, _DockerCLI):
                result = self._reader.read(
                    query, endpoint=endpoint, image_id=image_id, deadline=deadline, stop=stop
                )
            else:
                # Private injected readers are cooperative; production polls its captured CLI.
                result = self._reader.read(
                    query,
                    endpoint=endpoint,
                    image_id=image_id,
                    deadline=min(deadline, time.monotonic() + 1),
                )
            stop.check()
            return result

        try:
            if stop is not None:
                stop.check()
            self._reader.check()
            version = _json(
                read("client.version", endpoint=profile.endpoint, image_id=None, deadline=deadline)
            )
            if not isinstance(version, str) or not re.fullmatch(r"29\.[0-9]+\.[0-9]+", version):
                raise DockerBackendFault("docker_unsupported")
            engine = _Engine.model_validate(
                _json(
                    read("engine.info", endpoint=profile.endpoint, image_id=None, deadline=deadline)
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
                    read(
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
                    read("engine.info", endpoint=profile.endpoint, image_id=None, deadline=deadline)
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
