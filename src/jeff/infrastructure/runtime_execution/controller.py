"""Exact-owned controller state, serialization, journaling and narrow workflow delegates."""

import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from jeff.application.execution import (
    AuthorityResult,
    CancellableDockerVerifier,
    DockerBackendFault,
    DockerCompletionStore,
    DockerExecutionResult,
    DockerJobOutcome,
    DockerLaunchEvent,
    DockerLaunchIntent,
    DockerLaunchJournal,
    DockerLifecycleBackend,
    DockerOutputCapture,
    DockerOutputSink,
    DockerOwnedContainer,
    DockerTargetDeclaration,
    DockerTargetVerifier,
    ExecutionStopRecord,
    ExecutionStopSignal,
    ExecutionStopStore,
    GuardExecutionVerifier,
    PreparedInputVerifier,
    VerifiedDockerTarget,
)

from . import cancellation, cleanup, completion, create, policy, run
from .arguments import ownership_labels
from .policy import _mapping
from .session import _Session


class DockerJobController:
    """Journal mutations and retain exact local ownership until cleanup is confirmed.

    Unknown outcomes quarantine the controller rather than granting another attempt.
    Successful cleanup does not by itself reconcile an uncertain job completion.
    """

    def __init__(
        self,
        verifier: DockerTargetVerifier,
        backend: DockerLifecycleBackend,
        inputs: PreparedInputVerifier,
        journal: DockerLaunchJournal,
        *,
        owner_instance_id: UUID,
        guards: GuardExecutionVerifier | None = None,
    ) -> None:
        self.verifier, self.backend, self.inputs, self.journal = verifier, backend, inputs, journal
        self.owner_instance_id = owner_instance_id
        self.guards = guards
        self._mutex = threading.Lock()
        self._used_claims: set[UUID] = set()
        self._sessions: dict[str, _Session] = {}
        self._declarations: dict[UUID, DockerTargetDeclaration] = {}
        self._quarantined = False
        self._journal_lost = False

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if not self._mutex.acquire(blocking=False):
            raise DockerBackendFault("docker_busy")
        try:
            yield
        finally:
            self._mutex.release()

    @staticmethod
    def _deadline(deadline: float) -> None:
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")

    @classmethod
    def _budgets(cls, deadline: float, cleanup_deadline: float) -> None:
        cls._deadline(deadline)
        if not math.isfinite(cleanup_deadline) or not deadline <= cleanup_deadline <= deadline + 10:
            raise DockerBackendFault("docker_unsafe_configuration")

    def _record(
        self,
        intent: DockerLaunchIntent,
        phase: str,
        *,
        deadline: float,
        handle: DockerOwnedContainer | None = None,
        exit_code: int | None = None,
        observed_container_id: str | None = None,
    ) -> None:
        event = DockerLaunchEvent.model_validate(
            {
                "intent": intent,
                "phase": phase,
                "container_id": handle.container_id if handle else None,
                "created_at": handle.created_at if handle else None,
                "exit_code": exit_code,
                "observed_container_id": observed_container_id,
            }
        )
        try:
            self._deadline(deadline)
            self.journal.record(event, deadline=deadline)
            self._deadline(deadline)
        except Exception:
            self._quarantined = self._journal_lost = True
            raise DockerBackendFault("docker_journal_unconfirmed") from None

    def _unknown(
        self,
        intent: DockerLaunchIntent,
        deadline: float,
        handle: DockerOwnedContainer | None = None,
        observed_container_id: str | None = None,
    ) -> None:
        self._quarantined = True
        try:
            self._record(
                intent,
                "unknown",
                deadline=deadline,
                handle=handle,
                observed_container_id=observed_container_id if handle is None else None,
            )
        except DockerBackendFault:
            pass  # Keep memory quarantine even when uncertainty cannot be durably recorded.

    def _engine(self, intent: DockerLaunchIntent, deadline: float) -> None:
        self._deadline(deadline)
        if self.backend.identity != intent.target.binary:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        if (
            self.backend.engine_id(intent.target.endpoint, deadline=deadline)
            != intent.target.engine_id
        ):
            raise DockerBackendFault("docker_ownership_unconfirmed")

    def _read(
        self,
        intent: DockerLaunchIntent,
        container_id: str,
        deadline: float,
        created_at: str | None = None,
    ) -> dict[str, object]:
        self._engine(intent, deadline)
        raw = self.backend.inspect(intent.target.endpoint, container_id, deadline=deadline)
        self._engine(intent, deadline)
        labels = _mapping(_mapping(raw.get("Config")).get("Labels"))
        if (
            raw.get("Id") != container_id
            or any(labels.get(k) != v for k, v in ownership_labels(intent).items())
            or not isinstance(raw.get("Created"), str)
            or (created_at is not None and raw.get("Created") != created_at)
        ):
            raise DockerBackendFault("docker_ownership_unconfirmed")
        return raw

    @staticmethod
    def _safe_config(handle: DockerOwnedContainer, raw: dict[str, object]) -> None:
        return policy._safe_config(handle, raw)

    def _session(self, handle: DockerOwnedContainer) -> _Session:
        session = self._sessions.get(handle.container_id)
        if session is None or session.handle is not handle:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        return session

    def inspect_owned(self, handle: DockerOwnedContainer, *, deadline: float) -> dict[str, object]:
        with self._operation():
            if self._session(handle).removed:
                raise DockerBackendFault("docker_ownership_unconfirmed")
            return self._read(handle.intent, handle.container_id, deadline, handle.created_at)

    def _verify(
        self,
        declaration: DockerTargetDeclaration,
        deadline: float,
        stop: ExecutionStopSignal | None,
    ) -> VerifiedDockerTarget:
        if stop is not None:
            if not isinstance(self.verifier, CancellableDockerVerifier):
                raise DockerBackendFault("docker_unsafe_configuration")
            return self.verifier.verify_target_cancellable(declaration, stop, deadline=deadline)
        return self.verifier.verify_target(declaration, deadline=deadline)

    def create(
        self,
        claim: AuthorityResult,
        declaration: DockerTargetDeclaration,
        *,
        deadline: float,
        cleanup_deadline: float,
        stop: ExecutionStopSignal | None = None,
    ) -> DockerOwnedContainer:
        """Spend first-response permission once, journaling intent before Docker Create.

        The claim stays spent even if verification or mutation fails. A returned
        handle is local session identity, never permission to adopt a stored CID.
        """
        return create.create(
            self,
            claim,
            declaration,
            deadline=deadline,
            cleanup_deadline=cleanup_deadline,
            stop=stop,
        )

    def _cleanup(
        self, session: _Session, deadline: float, *, grace_deadline: float | None = None
    ) -> bool:
        return cleanup._cleanup(self, session, deadline, grace_deadline=grace_deadline)

    def terminate_remove_owned(self, handle: DockerOwnedContainer, *, deadline: float) -> bool:
        with self._operation():
            return self._cleanup(self._session(handle), deadline)

    def run(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        *,
        deadline: float,
        cleanup_deadline: float,
        stop: ExecutionStopSignal | None = None,
    ) -> DockerJobOutcome:
        return run.run(
            self, handle, sink, deadline=deadline, cleanup_deadline=cleanup_deadline, stop=stop
        )

    def _observe_running(
        self, handle: DockerOwnedContainer, stop: ExecutionStopSignal, deadline: float
    ) -> None:
        stop.check()
        try:
            raw = self._read(
                handle.intent,
                handle.container_id,
                min(deadline, time.monotonic() + 0.5),
                handle.created_at,
            )
        except DockerBackendFault as error:
            if error.code == "docker_timeout":
                return  # Bounded read-only probe timed out; never invent running readiness.
            raise
        if _mapping(raw.get("State")).get("Running") is True:
            stop.mark_running()

    def finish_stop_owned(
        self,
        handle: DockerOwnedContainer,
        capture: DockerOutputCapture,
        stop: ExecutionStopSignal,
        store: ExecutionStopStore,
    ) -> ExecutionStopRecord:
        return cancellation.finish_stop_owned(self, handle, capture, stop, store)

    def complete(
        self,
        handle: DockerOwnedContainer,
        capture: DockerOutputCapture,
        store: DockerCompletionStore,
        *,
        deadline: float,
    ) -> DockerExecutionResult:
        """One-shot finalization of this controller's actual run, never a DTO setter.

        Lost acknowledgement fences this caller; only facts inspection may discover
        a committed finish. In particular, never write unknown over that finish.
        """
        return completion.complete(self, handle, capture, store, deadline=deadline)

    def _declaration(self, handle: DockerOwnedContainer) -> dict[str, object]:
        return self._declarations[handle.intent.attempt_id].model_dump(mode="python")
