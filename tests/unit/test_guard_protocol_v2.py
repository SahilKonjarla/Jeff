"""JFG2 data contracts distinguish semantic cause without trusting raw exit codes."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    GuardV2Frame,
    GuardV2ProtocolFault,
    GuardV2Terminal,
    decode_guard_v2_frame,
    encode_guard_v2_frame,
)


def test_terminal_has_independent_network_order_vector() -> None:
    frame = GuardV2Terminal(
        nonce=UUID(int=1),
        manifest_digest="11" * 32,
        sequence=1,
        observed_ns=1100,
        cause="workload_exit",
        exit_code=124,
        stderr_eof=True,
        stderr_dropped=False,
    )
    expected = bytes.fromhex(
        "4a46473206"
        + "00000000000000000000000000000001"
        + "11" * 32
        + "0000000000000001"
        + "000000000000044c"
        + "0000000000000004"
        + "0101007c"
    )
    assert len(expected) == 81
    assert encode_guard_v2_frame(frame) == expected
    assert decode_guard_v2_frame(expected) == frame


def test_control_dispatch_and_stderr_preserve_identity_and_arbitrary_bytes() -> None:
    from jeff.application.execution import (
        GuardV2Abort,
        GuardV2Authorize,
        GuardV2Challenge,
        GuardV2Dispatch,
        GuardV2Stderr,
    )

    identity = {"nonce": UUID(int=1), "manifest_digest": "11" * 32}
    frames: list[GuardV2Frame] = [
        GuardV2Challenge.model_validate(
            identity | dict(issued_ns=1000, startup_deadline_ns=1100, max_runtime_ns=5000)
        ),
        GuardV2Authorize.model_validate(identity | dict(deadline_ns=1882)),
        GuardV2Abort.model_validate(identity),
        GuardV2Dispatch.model_validate(identity | dict(sequence=1, observed_ns=1050)),
        GuardV2Stderr.model_validate(identity | dict(sequence=2, data=b"\x00JFG2\xff\n")),
    ]
    for frame in frames:
        wire = encode_guard_v2_frame(frame)
        assert wire[:4] == b"JFG2"
        assert decode_guard_v2_frame(wire) == frame
    assert encode_guard_v2_frame(frames[0]) == bytes.fromhex(
        "4a46473201"
        + "00000000000000000000000000000001"
        + "11" * 32
        + "00000000000003e8"
        + "000000000000044c"
        + "0000000000001388"
    )
    assert encode_guard_v2_frame(frames[2])[-24:] == b"\0" * 24


@pytest.mark.parametrize(
    "cause, detail",
    [
        ("workload_exit", {"exit_code": 124}),
        ("workload_signal", {"signal_number": 9}),
        ("deadline_expired", {}),
        ("guard_fault", {"fault": "clock_regression"}),
    ],
)
def test_terminal_cause_is_explicit_and_not_raw_container_status(
    cause: str,
    detail: dict[str, object],
) -> None:
    terminal = GuardV2Terminal.model_validate(
        dict(
            nonce=UUID(int=1),
            manifest_digest="11" * 32,
            sequence=1,
            observed_ns=100,
            cause=cause,
            stderr_eof=False,
            stderr_dropped=True,
        )
        | detail
    )
    assert decode_guard_v2_frame(encode_guard_v2_frame(terminal)) == terminal
    assert not terminal.logical_stderr_complete


@pytest.mark.parametrize(
    "fields",
    [
        {"cause": "deadline_expired", "exit_code": 124},
        {"cause": "workload_exit"},
        {"cause": "workload_signal", "signal_number": 0},
        {"cause": "guard_fault"},
        {"cause": "guard_fault", "fault": "invented"},
        {"cause": "workload_exit", "exit_code": True},
        {"cause": "deadline_expired", "stderr_eof": 1},
        {"cause": "deadline_expired", "sequence": 0},
    ],
)
def test_invalid_terminal_claim_is_never_repaired(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GuardV2Terminal.model_validate(
            dict(
                nonce=UUID(int=1),
                manifest_digest="11" * 32,
                sequence=1,
                observed_ns=100,
                stderr_eof=True,
                stderr_dropped=False,
            )
            | fields
        )


def test_terminal_reserved_bits_lengths_and_jfg1_are_rejected() -> None:
    frame = GuardV2Terminal(
        nonce=UUID(int=1),
        manifest_digest="11" * 32,
        sequence=1,
        observed_ns=1100,
        cause="deadline_expired",
        stderr_eof=True,
        stderr_dropped=False,
    )
    wire = encode_guard_v2_frame(frame)
    for invalid in (
        wire[:-1],
        wire + b"x",
        b"JFG1" + wire[4:],
        wire[:78] + b"\x04" + wire[79:],
        wire[:-1] + b"\x01",
    ):
        with pytest.raises(GuardV2ProtocolFault) as refused:
            decode_guard_v2_frame(invalid)
        assert str(refused.value) == "Invalid JFG2 protocol input."


@pytest.mark.parametrize(
    "fields",
    [
        {"data": b""},
        {"data": b"x" * 4097},
        {"data": "SYNTHETIC_PRIVATE"},
        {"data": bytearray(b"x")},
        {"sequence": True},
        {"sequence": 0},
        {"nonce": UUID(int=0)},
    ],
)
def test_stderr_claims_are_strict_bounded_and_do_not_echo_values(fields: dict[str, object]) -> None:
    from jeff.application.execution import GuardV2Stderr

    with pytest.raises(ValidationError) as refused:
        GuardV2Stderr.model_validate(
            dict(
                nonce=UUID(int=1),
                manifest_digest="11" * 32,
                sequence=1,
                data=b"stderr",
            )
            | fields
        )
    assert "SYNTHETIC_PRIVATE" not in str(refused.value)


def test_reserved_control_words_oversized_headers_and_old_magic_refuse() -> None:
    from jeff.application.execution import GuardV2Abort, guard_v2_frame_size

    frame = encode_guard_v2_frame(GuardV2Abort(nonce=UUID(int=1), manifest_digest="11" * 32))
    for data in (frame[:-1] + b"\x01", b"JFG1" + frame[4:], frame + frame):
        with pytest.raises(GuardV2ProtocolFault):
            decode_guard_v2_frame(data)
    bad_header = frame[:4] + b"\x05" + frame[5:61] + (4097).to_bytes(8, "big") + b"\0" * 8
    with pytest.raises(GuardV2ProtocolFault):
        guard_v2_frame_size(bad_header)
