"""Installation-owned JFG2 resources. No discovery, downloads, chmod or executable invocation."""

import hashlib
import os
import stat
import time
from pathlib import Path

from jeff.application.execution import (
    DockerBackendFault,
    ExecutionManifest,
    GuardedExecutionManifest,
    GuardExecutionArtifact,
    GuardExecutionPolicy,
)
from jeff.infrastructure.docker_cli.transport import _safe_ancestors

_RESOURCE = Path(__file__).resolve().parent
# Build-pinned installation resources; configuration cannot introduce arbitrary helpers.
_ARTIFACTS = {
    "linux/arm64": (
        "7d86843d91d42ffa3bb53d1823d33fbde9397e5d473671b083560e6ba559406c",
        9888,
        "cf66160cb6e8b0c792af7ebe6bbb9d41ea5732af1f6ef01af203d6e576373849",
    ),
    "linux/amd64": (
        "7fde0150f7bb0fad5e76e70cc77b22416ba26d64d892e3a22ee6b9347c5aecf1",
        10048,
        "8065cc245d98b80cee958a3add26b51335a7ec7d1804b052a8bc254e2977d973",
    ),
}


def resource_path(artifact: GuardExecutionArtifact) -> Path:
    return _RESOURCE / artifact.filename


def artifact_path(artifact: GuardExecutionArtifact, *, deadline: float) -> Path:
    if time.monotonic() >= deadline:
        raise DockerBackendFault("docker_timeout")
    if (artifact.sha256, artifact.size, artifact.workload_filter_sha256) != _ARTIFACTS[
        artifact.platform
    ]:
        raise DockerBackendFault("docker_unsafe_configuration")
    path = resource_path(artifact)
    try:
        _safe_ancestors(path.parent, deadline=deadline)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not (
                stat.S_ISREG(before.st_mode)
                and before.st_uid in (0, os.geteuid())
                and before.st_nlink == 1
                and not before.st_mode & 0o022
                and before.st_mode & 0o111
                and before.st_size == artifact.size
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(fd, 65536):
                if time.monotonic() >= deadline:
                    raise DockerBackendFault("docker_timeout")
                total += len(chunk)
                if total > artifact.size:
                    raise DockerBackendFault("docker_unsafe_configuration")
                digest.update(chunk)
            after, named = os.fstat(fd), path.lstat()

            def signature(info: os.stat_result) -> tuple[int, ...]:
                return (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_uid,
                    info.st_gid,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )

            if (
                signature(before) != signature(after)
                or signature(after) != signature(named)
                or total != artifact.size
                or digest.hexdigest() != artifact.sha256
            ):
                raise DockerBackendFault("docker_unsafe_configuration")
        finally:
            os.close(fd)
    except OSError:
        raise DockerBackendFault("docker_unsafe_configuration") from None
    return path


class InstalledGuardVerifier:
    def __init__(self, policy: GuardExecutionPolicy) -> None:
        self.policy = policy

    def prepare(self, original: ExecutionManifest, *, deadline: float) -> GuardedExecutionManifest:
        try:
            action = self.policy.wrap(original)
        except ValueError:
            raise DockerBackendFault("docker_unsafe_configuration") from None
        self.check(action, deadline=deadline)
        return action

    def check(self, action: GuardedExecutionManifest, *, deadline: float) -> None:
        if self.policy.wrap(action.original) != action:
            raise DockerBackendFault("docker_target_mismatch")
        artifact_path(action.artifact, deadline=deadline)
