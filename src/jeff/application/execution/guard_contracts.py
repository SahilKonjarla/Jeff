"""Non-executable standalone guard declarations; legacy manifest identity is untouched."""

from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from jeff.domain.execution import fingerprint

from ._model import _Model
from .manifests import ExecutionManifest


class GuardArtifact(_Model):
    """Declared bytes/ABI/policy, not proof a build or kernel verification occurred."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: StrictInt = Field(gt=0, le=16777216)
    platform: Literal["linux/arm64", "linux/amd64"]
    workload_filter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol: Literal["jfg1"] = "jfg1"
    container_executable: Literal["/jeff-runtime/jeff-job-guard"] = "/jeff-runtime/jeff-job-guard"

    @property
    def filename(self) -> str:
        return "jeff-job-guard-" + self.platform.replace("/", "-")


class GuardedExecutionDraft(_Model):
    """Reviewable source contract only; no authority store accepts it as a manifest."""

    authority_kind: Literal["guarded_execution_draft_v1"] = "guarded_execution_draft_v1"
    readiness: Literal["source_contract_only"] = "source_contract_only"
    execution_available: Literal[False] = False
    launch_authority_issued: Literal[False] = False
    original: ExecutionManifest
    artifact: GuardArtifact
    startup_wait_ns: StrictInt = Field(gt=0, le=3600000000000)
    clock: Literal["CLOCK_BOOTTIME"] = "CLOCK_BOOTTIME"
    clock_rate_floor_ppm: StrictInt = Field(gt=0, le=1000000)
    deadline_margin_ns: StrictInt = Field(gt=0, le=3600000000000)

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
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))
