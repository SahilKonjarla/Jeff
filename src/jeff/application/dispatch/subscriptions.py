"""Bounded subscription requests, frames and owned activity-stream ports."""

from typing import Literal, Protocol, Self
from uuid import UUID, uuid4

from pydantic import Field, StrictInt, model_validator

from jeff.application.history import ActivityEvent, ActivityMeta, StreamPage
from jeff.application.projects import Failure
from jeff.application.runtime import PROTOCOL_REVISION, RuntimeIdentityData

from .scope import ProjectScope, _DispatchModel


class SubscriptionQuery(ProjectScope):
    query_id: UUID = Field(default_factory=uuid4)
    cursor: str | None = Field(default=None, max_length=8192)


class StreamQuery(SubscriptionQuery):
    """Internal bounded read; not a wire request or drain-admission override."""

    limit: StrictInt = Field(default=64, gt=0, le=64)


class StreamReply(_DispatchModel):
    page: StreamPage | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.page is None) == (self.error is None):
            raise ValueError("Stream read requires one page or failure.")
        return self


class SubscriptionRequest(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["subscribe"] = "subscribe"
    expected_instance_id: UUID
    remaining_ms: StrictInt = Field(default=30000, gt=0, le=30000)
    query: SubscriptionQuery


class StreamFrame(_DispatchModel):
    revision: StrictInt = Field(
        default=PROTOCOL_REVISION, ge=PROTOCOL_REVISION, le=PROTOCOL_REVISION
    )
    kind: Literal["ready", "event", "progress", "closed"]
    request_id: UUID
    identity: RuntimeIdentityData
    cursor: str | None = Field(default=None, max_length=8192)
    metadata: ActivityMeta | None = None
    event: ActivityEvent | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.kind == "closed") != (self.error is not None):
            raise ValueError("Only a terminal frame carries a fixed failure.")
        if (self.kind == "event") != (self.event is not None):
            raise ValueError("Only an event frame carries an event.")
        if (self.kind == "ready") != (self.metadata is not None):
            raise ValueError("Only setup carries initial metadata.")
        if self.kind != "closed" and self.cursor is None:
            raise ValueError("Data frames require a bounded resume position.")
        return self


class ActivityFault(Exception):
    """A fixed application setup refusal, never raw transport/OS details."""

    def __init__(self, failure: Failure) -> None:
        self.failure = failure
        self.code = failure.error.code
        super().__init__(failure.error.message)


class ActivityStream(Protocol):
    """One owned consumer; EOF is possible without a terminal frame or processing ACK."""

    ready: StreamFrame

    def __enter__(self) -> Self: ...
    def __exit__(self, *args: object) -> None: ...
    def receive(self, wait_seconds: float | None = None) -> StreamFrame | None: ...
    def close(self) -> None: ...


class ActivityClient(Protocol):
    """Open explicit subscriptions without reconnecting or persisting cursors implicitly."""

    def subscribe(self, query: SubscriptionQuery) -> ActivityStream: ...
