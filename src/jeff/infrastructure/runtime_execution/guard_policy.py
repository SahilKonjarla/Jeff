"""Exact guarded configuration; reuse legacy predicates only after validating the wrapper."""

from jeff.application.execution import (
    DockerBackendFault,
    DockerOwnedContainer,
    GuardedExecutionManifest,
    guard_arguments,
)

from .guard_artifacts import resource_path
from .policy import _mapping


def workload_config(handle: DockerOwnedContainer, raw: dict[str, object]) -> dict[str, object]:
    action = handle.intent.record.manifest
    assert isinstance(action, GuardedExecutionManifest)
    config, host = _mapping(raw.get("Config")), _mapping(raw.get("HostConfig"))
    arguments = list(guard_arguments(action))
    executable = action.artifact.container_executable
    if (
        raw.get("Path") != executable
        or raw.get("Args") != arguments
        or config.get("Entrypoint") != [executable]
        or config.get("Cmd") != arguments
        or any(
            config.get(k) is not True
            for k in ("OpenStdin", "AttachStdin", "AttachStdout", "AttachStderr", "StdinOnce")
        )
        or config.get("Tty") is not False
        or host.get("Init") is not False
    ):
        raise DockerBackendFault("docker_policy_mismatch")
    mounts = raw.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) not in (2, 3):
        raise DockerBackendFault("docker_policy_mismatch")
    entries = [_mapping(m) for m in mounts]
    guards = [m for m in entries if m.get("Destination") == executable]
    if len(guards) != 1:
        raise DockerBackendFault("docker_policy_mismatch")
    guard = guards[0]
    if (
        guard.get("Type") != "bind"
        or guard.get("Source") != str(resource_path(action.artifact))
        or guard.get("RW") is not False
        or guard.get("Propagation") != "rprivate"
    ):
        raise DockerBackendFault("docker_policy_mismatch")
    # Never mutate observed metadata. New argv/FD/mount predicates were checked
    # above; the existing strict checker still checks every workload/isolation fact.
    return raw | dict(
        Path=action.profile.executable,
        Args=list(action.original.argv[1:]),
        Config=config
        | dict(
            Entrypoint=[action.profile.executable],
            Cmd=list(action.original.argv[1:]),
            OpenStdin=False,
            AttachStdin=False,
        ),
        Mounts=[m for m in entries if m is not guard],
    )
