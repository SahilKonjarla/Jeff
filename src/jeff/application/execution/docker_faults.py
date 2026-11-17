"""Fixed Docker refusals and bounded public-safe diagnostic metadata."""

import re
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from ._model import _Model


class DockerFailureFacts(_Model):
    """Bounded allowlisted failure metadata; never raw CLI text or arbitrary field names."""

    returncode: StrictInt = Field(ge=-255, le=255)
    stdout_bytes: StrictInt = Field(ge=0, le=65536)
    stderr_bytes: StrictInt = Field(ge=0, le=65536)
    category: Literal["template_field_unavailable", "stderr_on_success", "other_cli_failure"]
    field_name: (
        Literal[
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
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.stdout_bytes + self.stderr_bytes > 65536 or (
            (self.category == "template_field_unavailable") != (self.field_name is not None)
        ):
            raise ValueError("Inconsistent bounded Docker failure metadata.")
        return self


DockerPolicyCheckId = Literal[
    "Config",
    "HostConfig",
    "Config.User",
    "Config.WorkingDir",
    "Config.Entrypoint",
    "Config.Cmd",
    "Config.Labels",
    "Config.StopSignal",
    "Config.OpenStdin",
    "Config.AttachStdin",
    "Config.Tty",
    "Config.Healthcheck",
    "Config.Env.type",
    "Config.Env.values",
    "Image",
    "Path",
    "Args",
    "HostConfig.NetworkMode",
    "HostConfig.IpcMode",
    "HostConfig.CgroupnsMode",
    "HostConfig.Memory",
    "HostConfig.MemorySwap",
    "HostConfig.NanoCpus",
    "HostConfig.PidsLimit",
    "HostConfig.ShmSize",
    "HostConfig.CapDrop",
    "HostConfig.Tmpfs",
    "HostConfig.ReadonlyRootfs",
    "HostConfig.Privileged",
    "HostConfig.AutoRemove",
    "HostConfig.PublishAllPorts",
    "HostConfig.CapAdd",
    "HostConfig.Devices",
    "HostConfig.DeviceRequests",
    "HostConfig.DeviceCgroupRules",
    "HostConfig.PortBindings",
    "HostConfig.Links",
    "HostConfig.VolumesFrom",
    "HostConfig.ExtraHosts",
    "HostConfig.Binds",
    "HostConfig.PidMode",
    "HostConfig.UTSMode",
    "HostConfig.Runtime",
    "HostConfig.SecurityOpt",
    "HostConfig.RestartPolicy",
    "HostConfig.RestartPolicy.Name",
    "HostConfig.LogConfig",
    "HostConfig.LogConfig.Type",
    "Mounts.type",
    "Mounts.count",
    "Mounts.entry",
    "Mounts.project.Type",
    "Mounts.project.Source",
    "Mounts.project.RW",
    "Mounts.project.Propagation",
    "Mounts.scratch.Type",
    "Mounts.scratch.RW",
    "State",
    "State.Status",
    "State.Running",
]


class DockerPolicyFacts(_Model):
    """First rejected predicate only: fixed identifier and booleans, never raw values.

    `type_ok` reports the expected structural kind, not acceptance. `matches` is
    always false; neither these facts nor synthetic fixtures establish live cause.
    """

    check_id: DockerPolicyCheckId
    present: StrictBool
    type_ok: StrictBool
    matches: StrictBool = False

    @model_validator(mode="after")
    def rejected(self) -> Self:
        if self.matches:
            raise ValueError("Policy failure facts must describe a rejected predicate.")
        return self


class DockerBackendFault(Exception):
    """Fixed non-secret refusal; never attach raw CLI output or parser exceptions."""

    def __init__(
        self,
        code: Literal[
            "docker_unavailable",
            "docker_unsafe_configuration",
            "docker_target_mismatch",
            "docker_unsupported",
            "docker_protocol_error",
            "docker_output_limit",
            "docker_output_failed",
            "docker_cancelled",
            "docker_timeout",
            "docker_command_failed",
            "docker_cleanup_unconfirmed",
            "docker_busy",
            "docker_authority_required",
            "docker_ownership_unconfirmed",
            "docker_policy_mismatch",
            "docker_mutation_unconfirmed",
            "docker_journal_unconfirmed",
            "docker_home_capacity",
        ],
        *,
        diagnostics: DockerFailureFacts | None = None,
        policy: DockerPolicyFacts | None = None,
        observed_container_id: str | None = None,
    ) -> None:
        if (
            observed_container_id is not None
            and re.fullmatch(r"[0-9a-f]{64}", observed_container_id) is None
        ):
            raise ValueError("Unconfirmed container identity must be bounded.")
        if policy is not None and code != "docker_policy_mismatch":
            raise ValueError("Policy facts require a policy refusal.")
        self.code = code
        self.policy = policy
        self.diagnostics = diagnostics
        self.observed_container_id = observed_container_id
        super().__init__(code)
