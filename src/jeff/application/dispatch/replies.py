"""Transient dispatch and scoped diagnostic replies; never an answer archive."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, model_validator

from jeff.application.compose_status import ComposeStatusSuccess
from jeff.application.history import ActivityPage, ActivityTrace, HistoryPage
from jeff.application.projects import Failure, Success
from jeff.application.questions import QuestionSuccess
from jeff.application.runtime import PROTOCOL_REVISION, RuntimeIdentityData
from jeff.application.storage import Receipt

from .scope import _DispatchModel


class DebugData(_DispatchModel):
    receipt: Receipt
    trace: ActivityTrace


class DiagnosticSuccess(_DispatchModel):
    ok: Literal[True] = True
    data: DebugData | HistoryPage
    error: None = None


class EvidenceReply(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    request_id: UUID
    identity: RuntimeIdentityData
    operation: Literal["trace", "replay", "debug", "history"]
    history: HistoryPage | None = None
    receipt: Receipt | None = None
    trace: ActivityTrace | None = None
    page: ActivityPage | None = None
    error: Failure | None = None
    missing_is_not_nonexecution_proof: Literal[True] = True

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.error is not None and any(
            x is not None for x in (self.receipt, self.trace, self.page, self.history)
        ):
            raise ValueError("An error cannot carry evidence.")
        if self.operation == "history":
            if any(x is not None for x in (self.receipt, self.trace, self.page)):
                raise ValueError("History carries only a summary page.")
            if self.error is None and self.history is None:
                raise ValueError("History requires a page or fixed error.")
        elif self.history is not None:
            raise ValueError("Only history carries a summary page.")
        if (self.receipt is None) != (self.trace is None):
            raise ValueError("Trace and receipt are one owner-thread snapshot.")
        if self.operation in ("trace", "debug") and self.page is not None:
            raise ValueError("Trace cannot carry a replay page.")
        if self.operation == "replay" and (self.receipt is not None or self.trace is not None):
            raise ValueError("Replay cannot carry a trace snapshot.")
        if self.operation == "replay" and self.error is None and self.page is None:
            raise ValueError("Replay requires a page or fixed failure.")
        return self


class DispatchReply(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    request_id: UUID
    identity: RuntimeIdentityData
    accepted: StrictBool | None = False
    result: Success | QuestionSuccess | ComposeStatusSuccess | Failure
    receipt: Receipt | None = None


class ReceiptReply(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    request_id: UUID
    identity: RuntimeIdentityData
    receipt: Receipt | None = None
    error: Failure | None = None
