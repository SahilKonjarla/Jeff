"""Fixed container create arguments, scratch limits and ownership labels."""

from jeff.application.execution import (
    DockerLaunchIntent,
    GuardedExecutionManifest,
    guard_arguments,
)

from .guard_artifacts import resource_path


def ownership_labels(intent: DockerLaunchIntent) -> dict[str, str]:
    return {
        "jeff.job": str(intent.record.job_id),
        "jeff.claim": str(intent.record.claim_id),
        "jeff.attempt": str(intent.attempt_id),
        "jeff.nonce": intent.nonce,
    }


def scratch_options(intent: DockerLaunchIntent) -> str:
    return (
        f"rw,noexec,nosuid,nodev,size={intent.record.manifest.profile.scratch_bytes},"
        "nr_inodes=4096,uid=65532,gid=65532,mode=0700"
    )


def create_arguments(intent: DockerLaunchIntent) -> tuple[str, ...]:
    """Fixed policy argv, not a pass-through Docker flag/shell surface."""
    p = intent.record.manifest.profile
    result = [
        "create",
        "--pull=never",
        "--name",
        "jeff-" + str(intent.attempt_id),
        "--read-only",
        "--network",
        "none",
        "--ipc",
        "private",
        "--cgroupns",
        "private",
        "--user",
        "65532:65532",
        "--workdir",
        "/project",
        "--entrypoint",
        p.executable,
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--log-driver",
        "none",
        "--restart",
        "no",
        "--no-healthcheck",
        "--stop-signal",
        "SIGTERM",
        "--memory",
        str(p.memory_bytes),
        "--memory-swap",
        str(p.memory_bytes),
        "--cpus",
        f"{p.cpu_millis / 1000:.3f}",
        "--pids-limit",
        str(p.pids),
        "--shm-size",
        "4194304",
        "--tmpfs",
        "/scratch:" + scratch_options(intent),
        "--mount",
        f"type=bind,source={intent.inputs.path},target=/project,readonly,bind-propagation=rprivate",
    ]
    for key, value in ownership_labels(intent).items():
        result.extend(("--label", f"{key}={value}"))
    for key, value in intent.target.effective_environment:
        result.extend(("--env", f"{key}={value}"))
    action = intent.record.manifest
    if isinstance(action, GuardedExecutionManifest):
        entrypoint = result.index("--entrypoint") + 1
        result[entrypoint] = action.artifact.container_executable
        result.extend(
            (
                "--init=false",
                "--interactive",
                "--attach=stdin",
                "--attach=stdout",
                "--attach=stderr",
                "--mount",
                f"type=bind,source={resource_path(action.artifact)},"
                f"target={action.artifact.container_executable},readonly,"
                "bind-propagation=rprivate",
            )
        )
        result.extend((p.image_id, *guard_arguments(action)))
    else:
        result.extend((p.image_id, *action.argv[1:]))
    return tuple(result)
