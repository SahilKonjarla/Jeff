"""Exact container configuration predicates and bounded refusal diagnostics."""

from typing import cast

from jeff.application.execution import (
    DockerBackendFault,
    DockerOwnedContainer,
    DockerPolicyFacts,
    GuardedExecutionManifest,
)

from .arguments import ownership_labels, scratch_options


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise DockerBackendFault("docker_policy_mismatch")
    return cast(dict[str, object], value)


def _policy_fault(check_id: str, present: bool, type_ok: bool) -> DockerBackendFault:
    # Construct facts only after the existing predicate rejected. Diagnostics must
    # never replace/refine acceptance or leak arbitrary names/values on failure.
    try:
        facts = DockerPolicyFacts.model_validate(
            {"check_id": check_id, "present": present, "type_ok": type_ok, "matches": False}
        )
    except (ValueError, TypeError):
        return DockerBackendFault("docker_policy_mismatch")
    return DockerBackendFault("docker_policy_mismatch", policy=facts)


def _policy_mapping(value: object, check_id: str, present: bool) -> dict[str, object]:
    try:
        return _mapping(value)
    except DockerBackendFault:
        raise _policy_fault(check_id, present, False) from None


def _state_fault(state: dict[str, object]) -> DockerBackendFault:
    key = "Status" if state.get("Status") != "created" else "Running"
    return _policy_fault(
        "State." + key,
        key in state,
        isinstance(state.get(key), str) if key == "Status" else type(state.get(key)) is bool,
    )


def _safe_config(handle: DockerOwnedContainer, raw: dict[str, object]) -> None:
    intent, manifest = handle.intent, handle.intent.record.manifest
    if isinstance(manifest, GuardedExecutionManifest):
        from .guard_policy import workload_config

        raw = workload_config(handle, raw)
    p = manifest.profile
    config = _policy_mapping(raw.get("Config"), "Config", "Config" in raw)
    host = _policy_mapping(raw.get("HostConfig"), "HostConfig", "HostConfig" in raw)
    expected = {
        "User": "65532:65532",
        "WorkingDir": "/project",
        "Entrypoint": [p.executable],
        "Cmd": list(manifest.argv[1:]),
        "Labels": ownership_labels(intent),
        "StopSignal": "SIGTERM",
    }
    if any(config.get(k) != v for k, v in expected.items()):
        key = next(k for k, v in expected.items() if config.get(k) != v)
        raise _policy_fault(
            "Config." + key, key in config, type(config.get(key)) is type(expected[key])
        )
    if any(config.get(k) is not False for k in ("OpenStdin", "AttachStdin", "Tty")):
        key = next(k for k in ("OpenStdin", "AttachStdin", "Tty") if config.get(k) is not False)
        raise _policy_fault("Config." + key, key in config, type(config.get(key)) is bool)
    if config.get("Healthcheck") not in (None, {"Test": ["NONE"]}):
        raise _policy_fault(
            "Config.Healthcheck",
            "Healthcheck" in config,
            isinstance(config.get("Healthcheck"), dict),
        )
    environment = config.get("Env")
    if not isinstance(environment, list) or not all(isinstance(v, str) for v in environment):
        raise _policy_fault("Config.Env.type", "Env" in config, False)
    wanted_env = sorted(f"{k}={v}" for k, v in intent.target.effective_environment)
    if sorted(environment) != wanted_env:
        raise _policy_fault("Config.Env.values", "Env" in config, True)
    if (
        raw.get("Image") != p.image_id
        or raw.get("Path") != p.executable
        or raw.get("Args") != list(manifest.argv[1:])
    ):
        commands = {"Image": p.image_id, "Path": p.executable, "Args": list(manifest.argv[1:])}
        key = next(k for k, v in commands.items() if raw.get(k) != v)
        raise _policy_fault(key, key in raw, type(raw.get(key)) is type(commands[key]))
    fixed = {
        "NetworkMode": "none",
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "Memory": p.memory_bytes,
        "MemorySwap": p.memory_bytes,
        "NanoCpus": p.cpu_millis * 1000000,
        "PidsLimit": p.pids,
        "ShmSize": 4194304,
        "CapDrop": ["ALL"],
        "Tmpfs": {"/scratch": scratch_options(intent)},
    }
    if any(
        host.get(k) != v or (type(v) is int and type(host.get(k)) is not int)
        for k, v in fixed.items()
    ):
        key = next(
            k
            for k, v in fixed.items()
            if host.get(k) != v or (type(v) is int and type(host.get(k)) is not int)
        )
        raise _policy_fault(
            "HostConfig." + key, key in host, type(host.get(key)) is type(fixed[key])
        )
    if host.get("ReadonlyRootfs") is not True or any(
        host.get(k) is not False for k in ("Privileged", "AutoRemove", "PublishAllPorts")
    ):
        key = (
            "ReadonlyRootfs"
            if host.get("ReadonlyRootfs") is not True
            else next(
                k
                for k in ("Privileged", "AutoRemove", "PublishAllPorts")
                if host.get(k) is not False
            )
        )
        raise _policy_fault("HostConfig." + key, key in host, type(host.get(key)) is bool)
    if any(
        host.get(k) not in (None, [], {})
        for k in (
            "CapAdd",
            "Devices",
            "DeviceRequests",
            "DeviceCgroupRules",
            "PortBindings",
            "Links",
            "VolumesFrom",
            "ExtraHosts",
            "Binds",
        )
    ):
        key = next(
            k
            for k in (
                "CapAdd",
                "Devices",
                "DeviceRequests",
                "DeviceCgroupRules",
                "PortBindings",
                "Links",
                "VolumesFrom",
                "ExtraHosts",
                "Binds",
            )
            if host.get(k) not in (None, [], {})
        )
        raise _policy_fault(
            "HostConfig." + key,
            key in host,
            host.get(key) is None or isinstance(host.get(key), (list, dict)),
        )
    if (
        host.get("PidMode") not in ("", "private")
        or host.get("UTSMode") not in ("", "private")
        or host.get("Runtime") not in ("", "runc")
    ):
        permitted = {
            "PidMode": ("", "private"),
            "UTSMode": ("", "private"),
            "Runtime": ("", "runc"),
        }
        key = next(k for k, v in permitted.items() if host.get(k) not in v)
        raise _policy_fault("HostConfig." + key, key in host, isinstance(host.get(key), str))
    if host.get("SecurityOpt") not in (["no-new-privileges"], ["no-new-privileges=true"]):
        raise _policy_fault(
            "HostConfig.SecurityOpt",
            "SecurityOpt" in host,
            isinstance(host.get("SecurityOpt"), list),
        )
    if (
        _policy_mapping(
            host.get("RestartPolicy"), "HostConfig.RestartPolicy", "RestartPolicy" in host
        ).get("Name")
        != "no"
        or _policy_mapping(host.get("LogConfig"), "HostConfig.LogConfig", "LogConfig" in host).get(
            "Type"
        )
        != "none"
    ):
        restart = _mapping(host.get("RestartPolicy"))
        if restart.get("Name") != "no":
            raise _policy_fault(
                "HostConfig.RestartPolicy.Name",
                "Name" in restart,
                isinstance(restart.get("Name"), str),
            )
        log = _mapping(host.get("LogConfig"))
        raise _policy_fault(
            "HostConfig.LogConfig.Type", "Type" in log, isinstance(log.get("Type"), str)
        )
    mounts = raw.get("Mounts")
    # Legacy --tmpfs is declared in HostConfig.Tmpfs (checked exactly above),
    # and need not be repeated in Mounts. Accept only the project bind or the
    # project bind plus an explicitly described scratch tmpfs, never extras.
    if not isinstance(mounts, list) or len(mounts) not in (1, 2):
        raise _policy_fault(
            "Mounts.count" if isinstance(mounts, list) else "Mounts.type",
            "Mounts" in raw,
            isinstance(mounts, list),
        )
    entries = [_policy_mapping(m, "Mounts.entry", True) for m in mounts]
    projects = [m for m in entries if m.get("Destination") == "/project"]
    scratches = [m for m in entries if m.get("Destination") == "/scratch"]
    # Count before selecting: a dictionary must not hide duplicate or foreign
    # destinations, and malformed destinations never become dictionary keys.
    if len(projects) != 1 or len(scratches) != len(mounts) - 1:
        raise _policy_fault(
            "Mounts.entry",
            True,
            all(isinstance(m.get("Destination"), str) for m in entries),
        )
    project, scratch = projects[0], scratches[0] if len(mounts) == 2 else {}
    if (
        project.get("Type") != "bind"
        or project.get("Source") != str(intent.inputs.path)
        or project.get("RW") is not False
        or project.get("Propagation") != "rprivate"
        or (len(mounts) == 2 and (scratch.get("Type") != "tmpfs" or scratch.get("RW") is not True))
    ):
        observations = (
            ("Mounts.project.Type", project, "Type", project.get("Type") == "bind", str),
            (
                "Mounts.project.Source",
                project,
                "Source",
                project.get("Source") == str(intent.inputs.path),
                str,
            ),
            ("Mounts.project.RW", project, "RW", project.get("RW") is False, bool),
            (
                "Mounts.project.Propagation",
                project,
                "Propagation",
                project.get("Propagation") == "rprivate",
                str,
            ),
            (
                "Mounts.scratch.Type",
                scratch,
                "Type",
                len(mounts) == 1 or scratch.get("Type") == "tmpfs",
                str,
            ),
            (
                "Mounts.scratch.RW",
                scratch,
                "RW",
                len(mounts) == 1 or scratch.get("RW") is True,
                bool,
            ),
        )
        check, entry, key, _matched, kind = next(row for row in observations if not row[3])
        raise _policy_fault(check, key in entry, isinstance(entry.get(key), kind))
