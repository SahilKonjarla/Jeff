"""Durable completion facts and immutable output/result digests."""

from hashlib import sha256
from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from jeff.domain.execution import fingerprint

from ._model import _Model
from .launch import DockerOwnedContainer
from .output import DockerJobOutcome, DockerOutputFacts, DockerOutputSnapshot
from .pytest_report import PytestResult


class DockerCompletion(_Model):
    """Trusted controller-to-SQL observation, not a public request or replay permit."""

    owned: DockerOwnedContainer
    outcome: DockerJobOutcome
    output: DockerOutputSnapshot
    pytest_result: PytestResult | None = None

    @model_validator(mode="after")
    def report_matches_outcome(self) -> Self:
        if self.pytest_result is not None and not self.confirmed:
            raise ValueError("An unconfirmed job cannot record a pytest result.")
        if (
            self.pytest_result is not None
            and self.pytest_result.report is not None
            and self.pytest_result.report.exit_status != self.outcome.exit_code
        ):
            raise ValueError("Pytest report does not match the job exit status.")
        return self

    @property
    def confirmed(self) -> bool:
        return (
            self.output.complete
            and not self.outcome.outcome_unknown
            and not self.outcome.timed_out
            and self.outcome.cleanup_verified
            and self.outcome.exit_code is not None
        )

    @property
    def output_facts(self) -> DockerOutputFacts:
        return DockerOutputFacts.model_validate(
            self.output.model_dump(exclude={"stdout", "stderr"})
        )

    @property
    def output_digest(self) -> str:
        # Unlike canonical metadata (32KiB), sanitized output can be1MiB.
        return sha256(self.output.model_dump_json().encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        return completion_digest(self.owned, self.outcome, self.output_facts, self.output_digest)


def completion_digest(
    owned: DockerOwnedContainer,
    outcome: DockerJobOutcome,
    facts: DockerOutputFacts,
    output_digest: str,
) -> str:
    return fingerprint(
        {
            "intent": owned.intent.fingerprint,
            "container": owned.container_id,
            "birth": owned.created_at,
            "outcome": outcome.model_dump(mode="json"),
            "facts": facts.model_dump(mode="json"),
            "output": output_digest,
        }
    )


class DockerExecutionResult(_Model):
    """Facts-only view; retired text is explicitly absent, not an empty capture."""

    owned: DockerOwnedContainer
    outcome: DockerJobOutcome
    output_facts: DockerOutputFacts
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["completed", "quarantined"]
    sequence: StrictInt = Field(ge=1, le=9223372036854775807)
    recorded_at_ms: StrictInt = Field(ge=0, le=9223372036854775807)
    payload_state: Literal["retained", "retired", "expired", "clock_unconfirmed"]
    retirement_reason: Literal["age", "capacity"] | None = None
    output: DockerOutputSnapshot | None
    pytest_result: PytestResult | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.payload_state == "retained") != (
            self.output is not None
        ) or self.completion_digest != completion_digest(
            self.owned, self.outcome, self.output_facts, self.output_digest
        ):
            raise ValueError("Inconsistent completion facts.")
        if (self.payload_state == "retired") != (self.retirement_reason is not None):
            raise ValueError("Inconsistent payload retirement.")
        if self.disposition == "completed" and (
            self.outcome.outcome_unknown
            or self.outcome.timed_out
            or not self.outcome.cleanup_verified
            or self.outcome.exit_code is None
            or self.output_facts.aborted
            or not (self.output_facts.stdout_eof and self.output_facts.stderr_eof)
        ):
            raise ValueError("Unconfirmed completion.")
        if self.output is not None:
            observed = DockerCompletion(owned=self.owned, outcome=self.outcome, output=self.output)
            if (
                observed.output_digest != self.output_digest
                or observed.output_facts != self.output_facts
            ):
                raise ValueError("Inconsistent retained output.")
        return self
