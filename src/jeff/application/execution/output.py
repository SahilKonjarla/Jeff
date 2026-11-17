"""Sanitized output snapshots, retained facts and descriptive job outcomes."""

from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from ._model import _Model

OutputGuardReason = Literal[
    "none", "sensitive_channel", "line_limit", "deferred_limit", "invalid_utf8"
]


class DockerOutputSnapshot(_Model):
    """Sanitized descriptive output, never completion or launch authority.

    Text is plain ASCII with LF only; visible escapes must not be interpreted again.
    EOF completeness is not controller/transport/cleanup confirmation.
    """

    policy: Literal["guarded_output_v1"] = "guarded_output_v1"
    retention_limit: StrictInt = Field(ge=1, le=1048576)
    stdout: str = Field(max_length=1048576)
    stderr: str = Field(max_length=1048576)
    retained_bytes: StrictInt = Field(ge=0, le=1048576)
    stdout_source_bytes: StrictInt = Field(ge=0, le=9223372036854775807)
    stderr_source_bytes: StrictInt = Field(ge=0, le=9223372036854775807)
    stdout_eof: StrictBool
    stderr_eof: StrictBool
    aborted: StrictBool
    truncated: StrictBool
    guard_reason: OutputGuardReason

    @model_validator(mode="after")
    def bounded_safe_text(self) -> Self:
        if (
            self.retained_bytes != len(self.stdout) + len(self.stderr)
            or self.retained_bytes > self.retention_limit
            or any(
                char != "\n" and not 32 <= ord(char) <= 126
                for text in (self.stdout, self.stderr)
                for char in text
            )
            or (not self.aborted and not (self.stdout_eof and self.stderr_eof))
        ):
            raise ValueError("Inconsistent bounded sanitized output.")
        return self

    @property
    def complete(self) -> bool:
        return self.stdout_eof and self.stderr_eof and not self.aborted


class DockerJobOutcome(_Model):
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    timed_out: bool = False
    outcome_unknown: bool = False
    cleanup_verified: bool = False


class DockerOutputFacts(_Model):
    """Original capture facts survive logical text retirement; never authority."""

    policy: Literal["guarded_output_v1"]
    retention_limit: StrictInt = Field(ge=1, le=1048576)
    retained_bytes: StrictInt = Field(ge=0, le=1048576)
    stdout_source_bytes: StrictInt = Field(ge=0, le=9223372036854775807)
    stderr_source_bytes: StrictInt = Field(ge=0, le=9223372036854775807)
    stdout_eof: StrictBool
    stderr_eof: StrictBool
    aborted: StrictBool
    truncated: StrictBool
    guard_reason: OutputGuardReason

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.retained_bytes > self.retention_limit or (
            not self.aborted and not (self.stdout_eof and self.stderr_eof)
        ):
            raise ValueError("Inconsistent captured output facts.")
        return self
