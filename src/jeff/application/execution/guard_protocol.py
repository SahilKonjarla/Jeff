"""Fixed standalone-helper protocol data; parsing never grants launch authority."""

import struct
from typing import Self
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from ._model import _Model

_INT64_MAX = 9223372036854775807
_FRAME = struct.Struct("!4sB16s32sQQQ")
GUARD_FRAME_BYTES = _FRAME.size


class GuardProtocolFault(ValueError):
    """Safe protocol rejection, not an execution refusal or ownership proof."""


class _GuardIdentity(_Model):
    nonce: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("nonce")
    @classmethod
    def nonzero(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("An empty challenge identity is invalid.")
        return value


class GuardChallenge(_GuardIdentity):
    issued_ns: StrictInt = Field(ge=0, le=_INT64_MAX)
    startup_deadline_ns: StrictInt = Field(gt=0, le=_INT64_MAX)
    max_runtime_ns: StrictInt = Field(gt=0, le=3600000000000)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.startup_deadline_ns <= self.issued_ns:
            raise ValueError("Startup deadline must follow the clock anchor.")
        if self.issued_ns + self.max_runtime_ns > _INT64_MAX:
            raise ValueError("Runtime deadline exceeds the clock representation.")
        return self


class GuardAuthorize(_GuardIdentity):
    deadline_ns: StrictInt = Field(gt=0, le=_INT64_MAX)


class GuardAbort(_GuardIdentity):
    pass


GuardFrame = GuardChallenge | GuardAuthorize | GuardAbort


def encode_guard_frame(frame: GuardFrame) -> bytes:
    try:
        frame = type(frame).model_validate(frame.model_dump())
        if isinstance(frame, GuardChallenge):
            operation, words = 1, (frame.issued_ns, frame.startup_deadline_ns, frame.max_runtime_ns)
        elif isinstance(frame, GuardAuthorize):
            operation, words = 2, (frame.deadline_ns, 0, 0)
        elif isinstance(frame, GuardAbort):
            operation, words = 3, (0, 0, 0)
        else:
            raise ValueError
        return _FRAME.pack(
            b"JFG1", operation, frame.nonce.bytes, bytes.fromhex(frame.manifest_digest), *words
        )
    except (ValueError, TypeError, AttributeError, struct.error):
        raise GuardProtocolFault("Invalid guard protocol input.") from None


def decode_guard_frame(data: bytes) -> GuardFrame:
    if type(data) is not bytes or len(data) != GUARD_FRAME_BYTES:
        raise GuardProtocolFault("Invalid guard protocol input.")
    try:
        magic, operation, nonce, digest, first, second, third = _FRAME.unpack(data)
        if magic != b"JFG1":
            raise ValueError
        identity = UUID(bytes=nonce)
        if operation == 1:
            return GuardChallenge(
                nonce=identity,
                manifest_digest=digest.hex(),
                issued_ns=first,
                startup_deadline_ns=second,
                max_runtime_ns=third,
            )
        if operation == 2 and second == third == 0:
            return GuardAuthorize(nonce=identity, manifest_digest=digest.hex(), deadline_ns=first)
        if operation == 3 and first == second == third == 0:
            return GuardAbort(nonce=identity, manifest_digest=digest.hex())
        raise ValueError
    except (ValueError, TypeError, struct.error):
        raise GuardProtocolFault("Invalid guard protocol input.") from None
