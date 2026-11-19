"""Pure JFG2 candidate receiver; performs no I/O, timing, verification or authority.

A real adapter must separately prove original transport ownership, endpoint/OFD
separation, descriptor protection and observed terminal/removal/output facts.
Neither calling feed_guard_stderr nor presenting a matching nonce proves those.
"""

from typing import Literal, NoReturn

from .guard_execution import GuardedExecutionManifest
from .guard_protocol_v2 import (
    GUARD_V2_HEADER_BYTES,
    GUARD_V2_STDERR_BYTES,
    GuardV2Authorize,
    GuardV2Challenge,
    GuardV2Dispatch,
    GuardV2ProtocolFault,
    GuardV2Stderr,
    GuardV2Terminal,
    decode_guard_v2_frame,
    guard_v2_frame_size,
)

GuardV2State = Literal["parked", "authorization_prepared", "dispatch_candidate",
                       "terminal_candidate", "complete_candidate", "unknown"]
_MAX = 9223372036854775807


class GuardV2Session:
    """Candidates only: no kernel exec/start/timeout/cleanup or grant is confirmed.

    Deadline wins a normal child-reap observation at or after the deadline.
    Clock faults remain faults, never inferred timeouts. Missing terminal data
    and abandoned partial frames permanently invalidate this receiver.
    """

    def __init__(self, manifest: GuardedExecutionManifest, challenge: GuardV2Challenge) -> None:
        self._manifest = GuardedExecutionManifest.model_validate(manifest.model_dump())
        self._challenge = GuardV2Challenge.model_validate(challenge.model_dump())
        if (self.challenge.manifest_digest != self.manifest.fingerprint
            or self.challenge.startup_deadline_ns - self.challenge.issued_ns
            != self.manifest.startup_wait_ns
            or self.challenge.max_runtime_ns
            != self.manifest.original.profile.timeout_seconds * 1000000000):
            raise GuardV2ProtocolFault("Invalid JFG2 session input.")
        self._state: GuardV2State = "parked"
        self._deadline: int | None = None
        self._last_ns = challenge.issued_ns
        self._next_sequence = 1
        self._buffer = bytearray()
        self._terminal: GuardV2Terminal | None = None

    @property
    def manifest(self) -> GuardedExecutionManifest:
        return self._manifest

    @property
    def challenge(self) -> GuardV2Challenge:
        return self._challenge

    @property
    def state(self) -> GuardV2State:
        return self._state

    @property
    def runtime_verified(self) -> Literal[False]:
        return False

    @property
    def launch_authority_issued(self) -> Literal[False]:
        return False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def terminal_candidate(self) -> GuardV2Terminal | None:
        return self._terminal

    def close(self) -> None:
        """Transport loss, not a retry or reconstruction of the original owner."""
        self._state, self._terminal = "unknown", None
        self._buffer.clear()

    def _fail(self) -> NoReturn:
        self.close()
        raise GuardV2ProtocolFault("Invalid JFG2 session input.")

    def prepare_authorization(self, *, host_now_ns: int, host_deadline_ns: int) -> GuardV2Authorize:
        """One candidate, anchored BEFORE this host sample; not a send or launch.

        Requires an independently justified relative clock floor. Delay consumes
        the original remaining host budget; neither absolute guest/host clock
        equality nor scheduling/hard wall-clock guarantees are established.
        """
        if self._state != "parked" or any(
            type(n) is not int or not 0 <= n <= _MAX for n in (host_now_ns, host_deadline_ns)
        ):
            self._fail()
        remaining = min(self.challenge.max_runtime_ns, host_deadline_ns - host_now_ns)
        remaining -= self.manifest.deadline_margin_ns
        duration = remaining * self.manifest.clock_rate_floor_ppm // 1000000
        if duration <= 0:
            self._fail()
        deadline = self.challenge.issued_ns + duration
        if deadline > _MAX:
            self._fail()
        self._deadline, self._state = deadline, "authorization_prepared"
        return GuardV2Authorize(nonce=self.challenge.nonce,
                                manifest_digest=self.manifest.fingerprint, deadline_ns=deadline)

    def accept_stdout(self, data: bytes) -> bytes:
        """Raw workload stdout never enters the trusted-status parser or advances state."""
        if type(data) is not bytes or len(data) > GUARD_V2_STDERR_BYTES:
            self._fail()
        return data

    def feed_guard_stderr(self, data: bytes) -> tuple[bytes, ...]:
        """Decode claimed guard-channel frames; yield ONLY child-stderr data.

        At most4096 input bytes/call, one bounded partial frame, and finite work
        per call. No raw/control bytes are retained as user output. Caller must
        separately establish channel provenance; this method cannot do so.
        """
        if (self._state in ("parked", "unknown", "complete_candidate", "terminal_candidate")
            or type(data) is not bytes or not 0 < len(data) <= GUARD_V2_STDERR_BYTES):
            self._fail()
        self._buffer.extend(data)
        output: list[bytes] = []
        try:
            while len(self._buffer) >= GUARD_V2_HEADER_BYTES:
                size = guard_v2_frame_size(bytes(self._buffer[:GUARD_V2_HEADER_BYTES]))
                if len(self._buffer) < size:
                    break
                frame = decode_guard_v2_frame(bytes(self._buffer[:size]))
                del self._buffer[:size]
                if (not isinstance(frame, (GuardV2Dispatch, GuardV2Stderr, GuardV2Terminal))
                    or frame.nonce != self.challenge.nonce
                    or frame.manifest_digest != self.manifest.fingerprint
                    or frame.sequence != self._next_sequence
                    or self._state == "terminal_candidate"):
                    self._fail()
                self._next_sequence += 1
                if isinstance(frame, GuardV2Stderr):
                    if self._state != "dispatch_candidate":
                        self._fail()
                    output.append(frame.data)
                    continue
                self._accept_observation(frame)
            if self._state == "terminal_candidate" and self._buffer:
                self._fail()
        except GuardV2ProtocolFault:
            self.close()
            raise GuardV2ProtocolFault("Invalid JFG2 session input.") from None
        return tuple(output)

    def _accept_observation(self, frame: GuardV2Dispatch | GuardV2Terminal) -> None:
        deadline = self._deadline
        if deadline is None:
            self._fail()
        assert deadline is not None
        if isinstance(frame, GuardV2Dispatch):
            if (self._state != "authorization_prepared"
                or not self._last_ns <= frame.observed_ns < deadline):
                self._fail()
            self._state = "dispatch_candidate"
        elif frame.cause in ("workload_exit", "workload_signal"):
            if (self._state != "dispatch_candidate"
                or not self._last_ns <= frame.observed_ns < deadline):
                self._fail()
        elif frame.cause == "deadline_expired":
            if frame.observed_ns < max(self._last_ns, deadline):
                self._fail()
        else:
            if (frame.fault == "startup_refused" and self._state == "dispatch_candidate"
                or frame.fault == "exec_failed" and self._state != "dispatch_candidate"):
                self._fail()
            if frame.fault != "clock_regression" and frame.observed_ns < self._last_ns:
                self._fail()
        self._last_ns = frame.observed_ns
        if isinstance(frame, GuardV2Terminal):
            self._terminal, self._state = frame, "terminal_candidate"

    def finish(self) -> GuardV2Terminal:
        """Clean protocol EOF is still not job completion, removal or complete logs."""
        if (self._state not in ("terminal_candidate", "complete_candidate")
            or self._buffer or self._terminal is None):
            self._fail()
        assert self._terminal is not None
        self._state = "complete_candidate"
        return self._terminal
