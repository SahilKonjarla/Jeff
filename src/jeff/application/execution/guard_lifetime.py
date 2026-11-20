"""Source-only guard transition model; no process, timer, kernel or execution authority."""

from typing import Literal
from uuid import uuid4

from .guard_contracts import GuardedExecutionDraft
from .guard_protocol import (
    GuardAuthorize,
    GuardChallenge,
    GuardProtocolFault,
    decode_guard_frame,
)

_INT64_MAX = 9223372036854775807
GuardState = Literal["parked", "armed", "exit_required"]


def candidate_guard_authorization(
    draft: GuardedExecutionDraft,
    challenge: GuardChallenge,
    *,
    host_now_ns: int,
    host_deadline_ns: int,
) -> GuardAuthorize:
    """Budget arithmetic, NOT authorization to launch.

    Requires an original live owner/transport, challenge issued before this host
    sample, and independently justified relative clock progress. These are not
    verified here. VM pauses/scheduling latency are not solved by this model.
    Delay consumes the guard-origin budget rather than restarting it on receipt.
    """
    if any(type(n) is not int or not 0 <= n <= _INT64_MAX for n in (host_now_ns, host_deadline_ns)):
        raise GuardProtocolFault("Invalid guard budget.")
    runtime = draft.original.profile.timeout_seconds * 1000000000
    if (
        challenge.manifest_digest != draft.fingerprint
        or challenge.max_runtime_ns != runtime
        or challenge.startup_deadline_ns - challenge.issued_ns != draft.startup_wait_ns
    ):
        raise GuardProtocolFault("Guard declaration mismatch.")
    remaining = min(runtime, host_deadline_ns - host_now_ns) - draft.deadline_margin_ns
    duration = remaining * draft.clock_rate_floor_ppm // 1000000
    if duration <= 0:
        raise GuardProtocolFault("Guard budget exhausted.")
    return GuardAuthorize(
        nonce=challenge.nonce,
        manifest_digest=challenge.manifest_digest,
        deadline_ns=challenge.issued_ns + duration,
    )


class GuardLifetime:
    """No restore/reconnect API; an armed source state is not guest-start evidence.

    Each instance creates its own fresh challenge. The eventual helper must exit
    PID1 on exit_required using a separately verified nonblocking kernel path.
    This Python model performs no such action and is never wired to production.
    """

    def __init__(self, draft: GuardedExecutionDraft, *, now_ns: int) -> None:
        draft = GuardedExecutionDraft.model_validate(draft.model_dump())
        if type(now_ns) is not int or not 0 <= now_ns <= _INT64_MAX:
            raise GuardProtocolFault("Invalid guard clock.")
        self._challenge = GuardChallenge(
            nonce=uuid4(),
            manifest_digest=draft.fingerprint,
            issued_ns=now_ns,
            startup_deadline_ns=now_ns + draft.startup_wait_ns,
            max_runtime_ns=draft.original.profile.timeout_seconds * 1000000000,
        )
        self._state: GuardState = "parked"
        self._last_ns = now_ns
        self._deadline_ns = self._challenge.startup_deadline_ns
        runtime = self._challenge.max_runtime_ns
        self._maximum_deadline_ns = now_ns + (
            (runtime - draft.deadline_margin_ns) * draft.clock_rate_floor_ppm // 1000000
        )

    @property
    def challenge(self) -> GuardChallenge:
        return self._challenge

    def close(self) -> None:
        """Owner channel loss, child completion or local error requires teardown."""
        self._state = "exit_required"

    def poll(self, now_ns: int) -> GuardState:
        if self._state == "exit_required":
            return self._state
        if type(now_ns) is not int or not self._last_ns <= now_ns <= _INT64_MAX:
            self.close()
        elif now_ns >= self._deadline_ns:
            self.close()
        else:
            self._last_ns = now_ns
        return self._state

    def accept(self, data: bytes, *, now_ns: int) -> bool:
        """Consume one valid proposal, not a live launch capability or kernel proof."""
        if self.poll(now_ns) != "parked":
            self.close()
            return False
        try:
            frame = decode_guard_frame(data)
        except GuardProtocolFault:
            self.close()
            return False
        challenge = self._challenge
        if (
            not isinstance(frame, GuardAuthorize)
            or frame.nonce != challenge.nonce
            or frame.manifest_digest != challenge.manifest_digest
            or not now_ns < frame.deadline_ns <= self._maximum_deadline_ns
        ):
            self.close()
            return False
        self._deadline_ns = frame.deadline_ns
        self._state = "armed"
        return True
