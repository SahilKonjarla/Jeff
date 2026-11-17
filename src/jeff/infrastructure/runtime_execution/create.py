"""Spend one fresh claim and journal before exact-owned container creation."""

import re
import secrets
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from jeff.application.execution import (
    AuthorityResult,
    DockerBackendFault,
    DockerLaunchIntent,
    DockerOwnedContainer,
    DockerTargetDeclaration,
    ExecutionStopSignal,
)

from .policy import _policy_mapping, _state_fault
from .session import _Session

if TYPE_CHECKING:
    from .controller import DockerJobController


def create(
    self: "DockerJobController",
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
    with self._operation():
        self._budgets(deadline, cleanup_deadline)
        if stop is not None:
            stop.require_scope(claim.record)
            stop.cap_cleanup(cleanup_deadline)
            stop.check()
        if self._quarantined or any(not s.removed for s in self._sessions.values()):
            raise DockerBackendFault("docker_busy")
        record = claim.record
        if (
            claim.launch_claim is None
            or claim.launch_claim != record.claim_id
            or record.claim_state != "claimed"
            or record.owner_instance_id != self.owner_instance_id
            or record.manifest.profile != declaration.profile
            or claim.launch_claim in self._used_claims
            or len(self._used_claims) >= 5000
        ):
            raise DockerBackendFault("docker_authority_required")
        self._used_claims.add(claim.launch_claim)
        intent: DockerLaunchIntent | None = None
        handle: DockerOwnedContainer | None = None
        observed_container_id: str | None = None
        intent_attempted = False
        try:
            target = self._verify(declaration, deadline, stop)
            if target.declaration_digest != declaration.fingerprint:
                raise DockerBackendFault("docker_target_mismatch")
            binding = self.inputs.verify(record.manifest, deadline=deadline)
            intent = DockerLaunchIntent(
                attempt_id=uuid4(),
                record=record,
                target=target,
                inputs=binding,
                nonce=secrets.token_hex(16),
            )
            self._declarations[intent.attempt_id] = declaration
            self.inputs.check(binding, record.manifest, deadline=deadline)
            self._engine(intent, deadline)
            if stop is not None:
                stop.check()
            intent_attempted = True
            self._record(intent, "create_intent", deadline=deadline)
            if stop is not None:
                stop.check()
            container_id = self.backend.create(intent, deadline=deadline)
            if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
                raise DockerBackendFault("docker_mutation_unconfirmed")
            observed_container_id = container_id
            if container_id in self._sessions:
                raise DockerBackendFault("docker_mutation_unconfirmed")
            raw = self._read(intent, container_id, deadline)
            handle = DockerOwnedContainer(
                intent=intent, container_id=container_id, created_at=cast(str, raw["Created"])
            )
            self._sessions[container_id] = _Session(handle)
            self._record(intent, "created", deadline=deadline, handle=handle)
            self._safe_config(handle, raw)
            state = _policy_mapping(raw.get("State"), "State", "State" in raw)
            if state.get("Status") != "created" or state.get("Running") is not False:
                raise _state_fault(state)
            return handle
        except Exception as error:
            if (
                not intent_attempted
                and isinstance(error, DockerBackendFault)
                and error.code == "docker_cancelled"
            ):
                # No journal/mutation was attempted; SQL still must prove the no-launch fence.
                raise
            self._quarantined = True
            if intent is not None:
                if (
                    isinstance(error, DockerBackendFault)
                    and error.observed_container_id is not None
                ):
                    observed_container_id = error.observed_container_id
                self._unknown(intent, cleanup_deadline, handle, observed_container_id)
            if handle is not None:
                self._cleanup(self._sessions[handle.container_id], cleanup_deadline)
            if isinstance(error, DockerBackendFault):
                raise
            raise DockerBackendFault("docker_mutation_unconfirmed") from None
