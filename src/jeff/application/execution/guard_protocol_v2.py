"""Bounded JFG2 candidates, not authenticated observations or launch permission.

Header: network-order !4sB16s32sQQQ (77 bytes): magic JFG2, opcode,
nonce, outer-manifest digest, then three words. Opcodes/words:
1 challenge: issued/startup-deadline/runtime; 2 authorize: deadline/0/0;
3 abort: 0/0/0; 4 child-dispatch: sequence/observed-time/0;
5 child-stderr: sequence/payload-length/0 plus1..4096 arbitrary bytes;
6 terminal: sequence/observed-time/4 plus !BBH(cause,flags,detail).
Terminal flags: bit0 logical stderr EOF, bit1 stderr dropped; others reserved.
Cause/detail codes are explicit below; container exit status is NOT a cause.
All observations share one increasing sequence, with exactly one terminal last.

Stdout remains raw workload data; stderr data payload is never nested protocol.
A transport adapter must establish an original, exclusive guard-owned channel,
reject stdout/stderr endpoint aliasing, protect descriptors from the workload,
and retain bounded nonblocking progress even when output consumers stop.
Magic bytes, matching nonce/digest and successful parsing do not prove those.
"""

import struct
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictBytes, StrictInt, field_validator, model_validator

from ._model import _Model

_MAX = 9223372036854775807
_HEADER = struct.Struct("!4sB16s32sQQQ")
GUARD_V2_HEADER_BYTES = _HEADER.size
GUARD_V2_STDERR_BYTES = 4096
GUARD_V2_MAX_FRAME_BYTES = GUARD_V2_HEADER_BYTES + GUARD_V2_STDERR_BYTES
GuardV2Cause = Literal["workload_exit", "workload_signal", "deadline_expired", "guard_fault"]
GuardV2Fault = Literal[
    "clock_regression", "startup_refused", "exec_failed", "transport_lost", "internal_error"
]
_CAUSES: dict[GuardV2Cause, int] = {
    "workload_exit": 1,
    "workload_signal": 2,
    "deadline_expired": 3,
    "guard_fault": 4,
}
_FAULTS: dict[GuardV2Fault, int] = {
    "clock_regression": 1,
    "startup_refused": 2,
    "exec_failed": 3,
    "transport_lost": 4,
    "internal_error": 5,
}


class GuardV2ProtocolFault(ValueError):
    """Safe protocol/model rejection, never proof of preclaim refusal."""


class _Identity(_Model):
    nonce: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("nonce")
    @classmethod
    def nonzero(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("An empty challenge identity is invalid.")
        return value


class GuardV2Challenge(_Identity):
    issued_ns: StrictInt = Field(ge=0, le=_MAX)
    startup_deadline_ns: StrictInt = Field(gt=0, le=_MAX)
    max_runtime_ns: StrictInt = Field(gt=0, le=3600000000000)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not 0 < self.startup_deadline_ns - self.issued_ns <= self.max_runtime_ns:
            raise ValueError("Invalid startup interval.")
        if self.issued_ns + self.max_runtime_ns > _MAX:
            raise ValueError("Runtime exceeds the clock representation.")
        return self


class GuardV2Authorize(_Identity):
    deadline_ns: StrictInt = Field(gt=0, le=_MAX)


class GuardV2Abort(_Identity):
    pass


class _Observation(_Identity):
    sequence: StrictInt = Field(ge=1, le=_MAX)


class GuardV2Dispatch(_Observation):
    """Claimed child dispatch, NOT proof of successful exec or kernel observation."""

    observed_ns: StrictInt = Field(ge=0, le=_MAX)


class GuardV2Stderr(_Observation):
    """Arbitrary child stderr payload, never recursively interpreted as protocol."""

    data: StrictBytes = Field(min_length=1, max_length=GUARD_V2_STDERR_BYTES)


class GuardV2Terminal(_Observation):
    """Claimed cause, separate from container status, cleanup and output completeness."""

    observed_ns: StrictInt = Field(ge=0, le=_MAX)
    cause: GuardV2Cause
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    signal_number: StrictInt | None = Field(default=None, ge=1, le=64)
    fault: GuardV2Fault | None = None
    stderr_eof: StrictBool
    stderr_dropped: StrictBool

    @model_validator(mode="after")
    def precise(self) -> Self:
        if (
            (self.exit_code is not None) != (self.cause == "workload_exit")
            or (self.signal_number is not None) != (self.cause == "workload_signal")
            or (self.fault is not None) != (self.cause == "guard_fault")
        ):
            raise ValueError("Terminal cause and detail disagree.")
        return self

    @property
    def logical_stderr_complete(self) -> bool:
        return self.stderr_eof and not self.stderr_dropped


GuardV2Frame = (
    GuardV2Challenge
    | GuardV2Authorize
    | GuardV2Abort
    | GuardV2Dispatch
    | GuardV2Stderr
    | GuardV2Terminal
)


def guard_v2_frame_size(header: bytes) -> int:
    """Bound one frame before buffering payload; this validates no sender identity."""
    try:
        if type(header) is not bytes or len(header) != GUARD_V2_HEADER_BYTES:
            raise ValueError
        magic, operation, _, _, _, second, third = _HEADER.unpack(header)
        if magic != b"JFG2" or operation not in (1, 2, 3, 4, 5, 6):
            raise ValueError
        if operation == 5:
            if not 1 <= second <= GUARD_V2_STDERR_BYTES or third:
                raise ValueError
            return GUARD_V2_HEADER_BYTES + int(second)
        if operation == 6:
            if third != 4:
                raise ValueError
            return GUARD_V2_HEADER_BYTES + 4
        return GUARD_V2_HEADER_BYTES
    except (ValueError, TypeError, struct.error):
        raise GuardV2ProtocolFault("Invalid JFG2 protocol input.") from None


def encode_guard_v2_frame(frame: GuardV2Frame) -> bytes:
    try:
        frame = type(frame).model_validate(frame.model_dump())
        payload = b""
        if isinstance(frame, GuardV2Challenge):
            operation, words = 1, (frame.issued_ns, frame.startup_deadline_ns, frame.max_runtime_ns)
        elif isinstance(frame, GuardV2Authorize):
            operation, words = 2, (frame.deadline_ns, 0, 0)
        elif isinstance(frame, GuardV2Abort):
            operation, words = 3, (0, 0, 0)
        elif isinstance(frame, GuardV2Dispatch):
            operation, words = 4, (frame.sequence, frame.observed_ns, 0)
        elif isinstance(frame, GuardV2Stderr):
            operation, words, payload = 5, (frame.sequence, len(frame.data), 0), frame.data
        elif isinstance(frame, GuardV2Terminal):
            detail = frame.exit_code if frame.exit_code is not None else frame.signal_number
            if frame.fault is not None:
                detail = _FAULTS[frame.fault]
            payload = struct.pack(
                "!BBH",
                _CAUSES[frame.cause],
                int(frame.stderr_eof) | (int(frame.stderr_dropped) << 1),
                detail if detail is not None else 0,
            )
            operation, words = 6, (frame.sequence, frame.observed_ns, 4)
        else:
            raise ValueError
        return (
            _HEADER.pack(
                b"JFG2", operation, frame.nonce.bytes, bytes.fromhex(frame.manifest_digest), *words
            )
            + payload
        )
    except (ValueError, TypeError, AttributeError, struct.error):
        raise GuardV2ProtocolFault("Invalid JFG2 protocol input.") from None


def decode_guard_v2_frame(data: bytes) -> GuardV2Frame:
    if (
        type(data) is not bytes
        or not GUARD_V2_HEADER_BYTES <= len(data) <= GUARD_V2_MAX_FRAME_BYTES
    ):
        raise GuardV2ProtocolFault("Invalid JFG2 protocol input.")
    try:
        if len(data) != guard_v2_frame_size(data[:GUARD_V2_HEADER_BYTES]):
            raise ValueError
        _, operation, nonce, digest, first, second, third = _HEADER.unpack(data[:77])
        identity = UUID(bytes=nonce)
        if operation == 1:
            return GuardV2Challenge(
                nonce=identity,
                manifest_digest=digest.hex(),
                issued_ns=first,
                startup_deadline_ns=second,
                max_runtime_ns=third,
            )
        if operation == 2 and second == third == 0:
            return GuardV2Authorize(nonce=identity, manifest_digest=digest.hex(), deadline_ns=first)
        if operation == 3 and first == second == third == 0:
            return GuardV2Abort(nonce=identity, manifest_digest=digest.hex())
        if operation == 4 and third == 0:
            return GuardV2Dispatch(
                nonce=identity, manifest_digest=digest.hex(), sequence=first, observed_ns=second
            )
        if operation == 5:
            return GuardV2Stderr(
                nonce=identity, manifest_digest=digest.hex(), sequence=first, data=data[77:]
            )
        if operation != 6:
            raise ValueError
        cause, flags, detail = struct.unpack("!BBH", data[77:])
        if flags & ~3:
            raise ValueError
        cause_name = next((key for key, code in _CAUSES.items() if code == cause), None)
        if cause_name is None:
            raise ValueError
        fault = next((key for key, code in _FAULTS.items() if code == detail), None)
        if cause_name == "deadline_expired" and detail:
            raise ValueError
        return GuardV2Terminal(
            nonce=UUID(bytes=nonce),
            manifest_digest=digest.hex(),
            sequence=first,
            observed_ns=second,
            cause=cause_name,
            exit_code=detail if cause_name == "workload_exit" else None,
            signal_number=detail if cause_name == "workload_signal" else None,
            fault=fault if cause_name == "guard_fault" else None,
            stderr_eof=bool(flags & 1),
            stderr_dropped=bool(flags & 2),
        )
    except (ValueError, TypeError, struct.error):
        raise GuardV2ProtocolFault("Invalid JFG2 protocol input.") from None
