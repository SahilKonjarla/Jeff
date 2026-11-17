"""Bounded Git observations attached to an already-owned durable receipt."""

import time
from uuid import UUID

from jeff.application.history import ActivityRecord, GitObservation

from .ports import DispatchStore


class _RequestObserver:
    def __init__(
        self, store: DispatchStore, request_id: UUID, owner: UUID, deadline: float
    ) -> None:
        self.store, self.request_id, self.owner, self.deadline = store, request_id, owner, deadline

    def record(self, observation: GitObservation) -> None:
        self.store.observe(
            ActivityRecord(
                request_id=self.request_id, owner_instance_id=self.owner, git=observation
            ),
            deadline=max(self.deadline, time.monotonic() + 2),
        )
