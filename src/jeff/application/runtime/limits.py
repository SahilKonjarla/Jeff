"""Reviewed protocol/path byte limits and finite runtime budget values."""

from typing import Self

from pydantic import Field, StrictInt, model_validator

from .base import _RuntimeModel

PROTOCOL_REVISION = 13


MAC_SOCKET_PATH_BYTES = 103


MAC_PATH_BYTES = 1023


MAC_NAME_BYTES = 255


class RuntimeLimits(_RuntimeModel):
    """Reviewed budgets only; future adapters must actually enforce them."""

    auth_bytes: StrictInt = Field(default=4096, gt=0, le=4294967295)
    request_bytes: StrictInt = Field(default=65536, gt=0, le=4294967295)
    response_bytes: StrictInt = Field(default=16777216, gt=0, le=4294967295)
    event_bytes: StrictInt = Field(default=65536, gt=0, le=4294967295)
    connections: StrictInt = Field(default=16, gt=0)
    active_requests: StrictInt = Field(default=4, gt=0)
    pending_requests: StrictInt = Field(default=16, gt=0)
    subscriptions: StrictInt = Field(default=8, gt=0)
    queued_events: StrictInt = Field(default=64, gt=0)
    auth_seconds: StrictInt = Field(default=5, gt=0)
    frame_seconds: StrictInt = Field(default=5, gt=0)
    request_seconds: StrictInt = Field(default=30, gt=0)
    sqlite_busy_seconds: StrictInt = Field(default=2, gt=0)
    start_seconds: StrictInt = Field(default=10, gt=0)
    stop_seconds: StrictInt = Field(default=35, gt=0)
    history_page_size: StrictInt = Field(default=50, gt=0)
    history_page_max: StrictInt = Field(default=100, gt=0)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not (
            self.auth_bytes <= self.request_bytes <= self.response_bytes
            and self.event_bytes <= self.response_bytes
            and self.active_requests <= self.connections
            and self.subscriptions <= self.connections
            and self.history_page_size <= self.history_page_max
            and self.stop_seconds >= self.request_seconds
        ):
            raise ValueError("Runtime budgets are inconsistent.")
        return self


DEFAULT_RUNTIME_LIMITS = RuntimeLimits()
