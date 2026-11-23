"""Project-scoped terminal inbox contracts; observing a delivery never acknowledges it."""

from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import UUID, uuid5

from pydantic import Field, StrictInt, model_validator

from ._model import _Model, absolute_path

WatchOperation = Literal["watch.job", "watch.pending", "watch.ack"]
WatchOutcome = Literal["passed", "failed", "cancelled", "timed_out", "interrupted", "not_started"]
WATCH_LIMIT = 128


def notification_id_for(project_id: UUID, job_id: UUID) -> UUID:
    return uuid5(project_id, f"jeff:terminal:v1:{job_id}")


class WatchScope(_Model):
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    operation: WatchOperation
    target: UUID | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        absolute_path(self.canonical_root)
        if (self.target is None) != (self.operation == "watch.pending"):
            raise ValueError("Watch creation and acknowledgement require a target.")
        return self


class JobNotification(_Model):
    notification_id: UUID
    job_id: UUID
    outcome: WatchOutcome
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (
            (self.exit_code is not None) != (self.outcome in {"passed", "failed"})
            or (self.outcome == "passed" and self.exit_code != 0)
            or (self.outcome == "failed" and self.exit_code == 0)
        ):
            raise ValueError("Terminal notification and exit status disagree.")
        return self


class WatchInbox(_Model):
    project_id: UUID
    notifications: tuple[JobNotification, ...] = Field(default=(), max_length=WATCH_LIMIT)
    watched_job_id: UUID | None = None
    notification_id: UUID | None = None
    acknowledged_id: UUID | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.watched_job_id is not None:
            if (
                self.notifications
                or self.acknowledged_id is not None
                or self.notification_id != notification_id_for(self.project_id, self.watched_job_id)
            ):
                raise ValueError("Invalid watch registration.")
        elif self.notification_id is not None or (self.acknowledged_id and self.notifications):
            raise ValueError("Invalid acknowledgement or inbox.")
        ids = {notice.notification_id for notice in self.notifications}
        if len(ids) != len(self.notifications) or any(
            notice.notification_id != notification_id_for(self.project_id, notice.job_id)
            for notice in self.notifications
        ):
            raise ValueError("Invalid terminal notification identity.")
        return self


class WatchStore(Protocol):
    def watch(self, scope: WatchScope, *, deadline: float) -> WatchInbox: ...
