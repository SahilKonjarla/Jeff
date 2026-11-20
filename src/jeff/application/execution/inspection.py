"""Consistent scoped observations of authority, completion and stop facts."""

from typing import Literal, Self

from pydantic import model_validator

from jeff.domain.execution import AuthorityState

from ._model import _Model
from .authority import AuthorityRecord
from .completion import DockerExecutionResult
from .output import DockerOutputFacts, DockerOutputSnapshot
from .stop_views import ExecutionStopView


class ExecutionInspection(_Model):
    record: AuthorityRecord
    completion_state: Literal[
        "not_claimed", "claim_only", "launch_without_result", "recorded", "stop_recorded"
    ]
    result: DockerExecutionResult | None = None
    stop: ExecutionStopView | None = None
    stop_output: DockerOutputSnapshot | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.completion_state == "stop_recorded":
            if (
                self.stop is None
                or self.stop.outcome in {None, "completed"}
                or self.result is not None
            ):
                raise ValueError("Stop terminal cannot manufacture ordinary completion.")
            if self.record.claim_id is None:
                if (
                    self.stop.evidence != "not_started"
                    or self.record.state != AuthorityState.INVALIDATED
                ):
                    raise ValueError("Unclaimed stop requires positive revocation.")
            elif self.record.claim_state != (
                "unknown" if self.stop.outcome == "unknown" else "finished"
            ):
                raise ValueError("Stop and consumed authority disagree.")
            if (self.stop.payload_state == "retained") != (self.stop_output is not None):
                raise ValueError("Stop payload disagrees with availability.")
            if (
                self.stop_output is not None
                and DockerOutputFacts.model_validate(
                    self.stop_output.model_dump(exclude={"stdout", "stderr"})
                )
                != self.stop.output_facts
            ):
                raise ValueError("Stop capture facts disagree.")
            return self
        if self.stop_output is not None:
            raise ValueError("Ordinary results use the original payload only.")
        if (self.completion_state == "not_claimed") != (self.record.claim_id is None):
            raise ValueError("Completion availability disagrees with claim facts.")
        if (self.completion_state == "recorded") != (self.result is not None):
            raise ValueError("Inconsistent completion availability.")
        if self.result is not None:
            original = self.result.owned.intent.record
            if (original.claim_id, original.approval_id, original.manifest_digest) != (
                self.record.claim_id,
                self.record.approval_id,
                self.record.manifest_digest,
            ):
                raise ValueError("Completion belongs to different authority.")
            expected = "finished" if self.result.disposition == "completed" else "unknown"
            if self.record.claim_state != expected:
                raise ValueError("Authority and result disagree.")
        elif self.record.claim_state == "finished":
            raise ValueError("Finished authority requires a confirmed result.")
        return self
