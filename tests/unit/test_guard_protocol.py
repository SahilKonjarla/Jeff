"""Standalone guard wire contract: fixed bytes, not a live Linux helper."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    GuardAbort,
    GuardAuthorize,
    GuardChallenge,
    GuardProtocolFault,
    decode_guard_frame,
    encode_guard_frame,
)


def test_guard_challenge_has_a_fixed_network_order_wire_vector() -> None:
    challenge = GuardChallenge(
        nonce=UUID(int=1),
        manifest_digest="11" * 32,
        issued_ns=1000,
        startup_deadline_ns=1100,
        max_runtime_ns=5000,
    )
    expected = bytes.fromhex(
        "4a46473101"
        + "00000000000000000000000000000001"
        + "11" * 32
        + "00000000000003e8"
        + "000000000000044c"
        + "0000000000001388"
    )
    assert len(expected) == 77
    assert encode_guard_frame(challenge) == expected
    assert decode_guard_frame(expected) == challenge


def test_authorize_and_abort_vectors_reserve_unused_words() -> None:
    prefix = "4a464731" + "02" + "00000000000000000000000000000001" + "11" * 32
    expected = bytes.fromhex(prefix + "000000000000075a" + "00" * 16)
    authorization = GuardAuthorize(nonce=UUID(int=1), manifest_digest="11" * 32, deadline_ns=1882)
    assert encode_guard_frame(authorization) == expected
    assert decode_guard_frame(expected) == authorization
    abort = GuardAbort(nonce=UUID(int=1), manifest_digest="11" * 32)
    assert encode_guard_frame(abort) == bytes.fromhex(prefix[:8] + "03" + prefix[10:] + "00" * 24)
    assert decode_guard_frame(encode_guard_frame(abort)) == abort
    with pytest.raises(GuardProtocolFault):
        decode_guard_frame(expected[:-1] + b"\x01")


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"x" * 76,
        b"x" * 78,
        b"x" * 100000,
        bytes.fromhex("4a46473201" + "00" * 72),
        bytes.fromhex("4a46473104" + "00" * 72),
        bytes.fromhex("4a46473103" + "00" * 16 + "11" * 32 + "00" * 24),
    ],
)
def test_malformed_frames_refuse_without_echoing_input(data: bytes) -> None:
    with pytest.raises(GuardProtocolFault) as refused:
        decode_guard_frame(data)
    assert str(refused.value) == "Invalid guard protocol input."


@pytest.mark.parametrize(
    "fields",
    [
        {"issued_ns": True},
        {"issued_ns": "1000"},
        {"issued_ns": -1},
        {"startup_deadline_ns": 1000},
        {"max_runtime_ns": 0},
        {"issued_ns": 9223372036854775807, "startup_deadline_ns": 9223372036854775807},
        {"nonce": UUID(int=0)},
        {"manifest_digest": "SYNTHETIC_PRIVATE"},
    ],
)
def test_challenge_fields_are_strict_and_bounded(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as refused:
        GuardChallenge.model_validate(
            dict(
                nonce=UUID(int=1),
                manifest_digest="11" * 32,
                issued_ns=1000,
                startup_deadline_ns=1100,
                max_runtime_ns=5000,
            )
            | fields
        )
    assert "SYNTHETIC_PRIVATE" not in str(refused.value)


def test_unsigned_wire_word_outside_clock_range_refuses() -> None:
    challenge = GuardChallenge(
        nonce=UUID(int=1),
        manifest_digest="11" * 32,
        issued_ns=1000,
        startup_deadline_ns=1100,
        max_runtime_ns=5000,
    )
    data = encode_guard_frame(challenge)
    with pytest.raises(GuardProtocolFault):
        decode_guard_frame(data[:53] + bytes.fromhex("8000000000000000") + data[61:])
    with pytest.raises(GuardProtocolFault):
        decode_guard_frame(data + data)
