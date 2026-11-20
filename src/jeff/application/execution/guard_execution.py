"""Distinct guarded action identity and explicit installation-private guard policy."""

import json
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from jeff.domain.execution import fingerprint

from ._model import _Model
from .manifests import ExecutionManifest
from .prepared_inputs import PreparedInputs
from .profiles import ExecutionProfile


class GuardExecutionArtifact(_Model):
    """JFG2 artifact declaration, not installation or kernel-verification evidence."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: StrictInt = Field(gt=0, le=16777216)
    platform: Literal["linux/arm64", "linux/amd64"]
    workload_filter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol: Literal["jfg2"] = "jfg2"
    container_executable: Literal["/jeff-runtime/jeff-job-guard"] = "/jeff-runtime/jeff-job-guard"

    @property
    def filename(self) -> str:
        return "jeff-job-guard-" + self.platform.replace("/", "-")


class GuardedExecutionManifest(_Model):
    """Distinct reviewed action; never a promotion of an existing approval/JFG1 draft.

    Original workload identity stays unchanged. Only the outer fingerprint can
    authorize the declared wrapper; actual artifact/transport checks remain required.
    """

    authority_kind: Literal["guarded_execution_v1"] = "guarded_execution_v1"
    verification: Literal["required_before_authority"] = "required_before_authority"
    original: ExecutionManifest
    artifact: GuardExecutionArtifact
    startup_wait_ns: StrictInt = Field(gt=0, le=3600000000000)
    clock: Literal["CLOCK_BOOTTIME"] = "CLOCK_BOOTTIME"
    clock_rate_floor_ppm: StrictInt = Field(gt=0, le=1000000)
    deadline_margin_ns: StrictInt = Field(gt=0, le=3600000000000)
    channel_layout: Literal["exclusive_guard_stderr_v1"] = "exclusive_guard_stderr_v1"
    terminal_delivery: Literal["bounded_nonblocking_or_unknown"] = "bounded_nonblocking_or_unknown"

    @model_validator(mode="after")
    def consistent(self) -> Self:
        runtime = self.original.profile.timeout_seconds * 1000000000
        if self.artifact.platform != self.original.profile.platform:
            raise ValueError("Guard and workload platforms disagree.")
        if self.startup_wait_ns > runtime or self.deadline_margin_ns >= runtime:
            raise ValueError("Guard budgets exceed the original runtime policy.")
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def profile(self) -> ExecutionProfile:
        return self.original.profile

    @property
    def argv(self) -> tuple[str, ...]:
        return self.original.argv

    @property
    def inputs(self) -> PreparedInputs:
        return self.original.inputs

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


type ExecutionAction = Annotated[
    ExecutionManifest | GuardedExecutionManifest, Field(discriminator="authority_kind")
]


def decode_execution_action(data: str) -> ExecutionManifest | GuardedExecutionManifest:
    value = json.loads(data)
    if isinstance(value, dict) and value.get("authority_kind") == "guarded_execution_v1":
        return GuardedExecutionManifest.model_validate_json(data)
    # Legacy rows retain their original strict model/defaults. Unknown kinds,
    # JFG1 drafts and guarded payloads lacking their discriminator fail closed.
    return ExecutionManifest.model_validate_json(data)


def guard_arguments(action: GuardedExecutionManifest) -> tuple[str, ...]:
    return (
        "--jfg2",
        action.fingerprint,
        str(action.startup_wait_ns),
        str(action.profile.timeout_seconds * 1000000000),
        str(action.clock_rate_floor_ppm),
        str(action.deadline_margin_ns),
        "--",
        *action.original.argv,
    )


def workload(action: ExecutionManifest | GuardedExecutionManifest) -> ExecutionManifest:
    return action.original if isinstance(action, GuardedExecutionManifest) else action


class GuardExecutionPolicy(_Model):
    """Explicit settings4 policy; no discovered artifact or invented timing defaults."""

    artifacts: tuple[GuardExecutionArtifact, ...] = Field(min_length=1, max_length=2)
    startup_wait_ns: StrictInt = Field(gt=0, le=3600000000000)
    clock_rate_floor_ppm: StrictInt = Field(gt=0, le=1000000)
    deadline_margin_ns: StrictInt = Field(gt=0, le=3600000000000)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({a.platform for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("Duplicate guard artifact platform.")
        return self

    def wrap(self, original: ExecutionManifest) -> GuardedExecutionManifest:
        artifacts = [a for a in self.artifacts if a.platform == original.profile.platform]
        if len(artifacts) != 1:
            raise ValueError("No exact guard for this platform.")
        return GuardedExecutionManifest(
            original=original,
            artifact=artifacts[0],
            startup_wait_ns=self.startup_wait_ns,
            clock_rate_floor_ppm=self.clock_rate_floor_ppm,
            deadline_margin_ns=self.deadline_margin_ns,
        )
