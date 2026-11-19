"""Ownership-only cleanup with one-shot mutations and confirmed-absence reconciliation."""

import time
from typing import TYPE_CHECKING

from jeff.application.execution import (
    CancellableDockerBackend,
    DockerBackendFault,
    GuardedDockerBackend,
    GuardedExecutionManifest,
)

from .policy import _mapping
from .session import _Session

if TYPE_CHECKING:
    from .controller import DockerJobController


def _cleanup(
    self: "DockerJobController",
    session: _Session,
    deadline: float,
    *,
    grace_deadline: float | None = None,
) -> bool:
    handle, intent = session.handle, session.handle.intent
    if session.guard_uncertain:
        return False  # Genuine captures remain observe-only; no endpoint commands or input closure.
    if session.removed:
        return True  # Confirmed prior removal is a fact, never a repeated mutation.
    try:
        if session.remove_attempted:
            self._engine(intent, deadline)
            if not self.backend.absent(
                intent.target.endpoint, handle.container_id, deadline=deadline
            ):
                raise DockerBackendFault("docker_cleanup_unconfirmed")
            self._engine(intent, deadline)
            session.removed = True
            self._record(intent, "cleaned", deadline=deadline, handle=handle)
            return True
        raw = self._read(intent, handle.container_id, deadline, handle.created_at)
        state = _mapping(raw.get("State"))
        if state.get("Running") is True:
            if not session.stop_attempted:
                session.stop_attempted = True
                try:
                    if grace_deadline is not None:
                        if not isinstance(self.backend, CancellableDockerBackend):
                            raise DockerBackendFault("docker_unsafe_configuration")
                        self.backend.stop_gracefully(
                            intent.target.endpoint,
                            handle.container_id,
                            grace_seconds=max(0, min(5, int(grace_deadline - time.monotonic()))),
                            deadline=deadline,
                        )
                    else:
                        self.backend.stop(
                            intent.target.endpoint, handle.container_id, deadline=deadline
                        )
                except Exception:
                    self._unknown(intent, deadline, handle)
                    if isinstance(intent.record.manifest, GuardedExecutionManifest):
                        session.guard_uncertain = True
                        return False
            raw = self._read(intent, handle.container_id, deadline, handle.created_at)
            if _mapping(raw.get("State")).get("Running") is True:
                if not session.kill_attempted:
                    session.kill_attempted = True
                    try:
                        self.backend.kill(
                            intent.target.endpoint, handle.container_id, deadline=deadline
                        )
                    except Exception:
                        self._unknown(intent, deadline, handle)
                        if isinstance(intent.record.manifest, GuardedExecutionManifest):
                            session.guard_uncertain = True
                            return False
                raw = self._read(intent, handle.container_id, deadline, handle.created_at)
        if _mapping(raw.get("State")).get("Running") is not False:
            raise DockerBackendFault("docker_cleanup_unconfirmed")
        final_state = _mapping(raw.get("State"))
        if (
            grace_deadline is not None
            and session.start_recorded
            and session.exit_code is None
            and final_state.get("Status") == "exited"
        ):
            code = final_state.get("ExitCode")
            if type(code) is not int or not 0 <= code <= 255:
                raise DockerBackendFault("docker_mutation_unconfirmed")
            session.exit_code = code
            self._record(intent, "exited", deadline=deadline, handle=handle, exit_code=code)
        self._engine(intent, deadline)
        session.remove_attempted = True
        self.backend.remove(intent.target.endpoint, handle.container_id, deadline=deadline)
        if not self.backend.absent(intent.target.endpoint, handle.container_id, deadline=deadline):
            raise DockerBackendFault("docker_cleanup_unconfirmed")
        self._engine(intent, deadline)
        session.removed = True
        try:
            self._record(intent, "cleaned", deadline=deadline, handle=handle)
        except DockerBackendFault:
            # Keep durable uncertainty as well as memory quarantine when time remains.
            self._unknown(intent, deadline, handle)
        if isinstance(intent.record.manifest, GuardedExecutionManifest):
            if not isinstance(
                self.backend, GuardedDockerBackend
            ) or not self.backend.release_guarded(handle, deadline=deadline):
                session.guard_uncertain = True
                self._unknown(intent, deadline, handle)
                return False
        return True
    except Exception:
        if isinstance(intent.record.manifest, GuardedExecutionManifest):
            session.guard_uncertain = True
        self._unknown(intent, deadline, handle)
        return False
