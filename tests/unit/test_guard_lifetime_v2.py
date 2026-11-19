"""Source-only JFG2 receiver state: candidates are never runtime/control authority."""

from pathlib import Path
from uuid import UUID

import pytest

from jeff.application.execution import (
    ExecutionManifest,
    ExecutionProfile,
    GuardedExecutionManifest,
    GuardExecutionArtifact,
    GuardV2Challenge,
    GuardV2Dispatch,
    GuardV2ProtocolFault,
    GuardV2Session,
    GuardV2Stderr,
    GuardV2Terminal,
    PreparedFile,
    PreparedInputs,
    encode_guard_v2_frame,
)


@pytest.fixture
def session() -> GuardV2Session:
    manifest = GuardedExecutionManifest(
        original=ExecutionManifest(
            profile=ExecutionProfile(
                name="python", project_id=UUID(int=1), canonical_root=Path("/synthetic/project"),
                engine_id="synthetic-engine", endpoint="unix:///synthetic/docker.sock",
                image_id="sha256:" + "1" * 64, platform="linux/arm64", toolchain="Python3.12",
                executable="/usr/local/bin/python", timeout_seconds=5,
            ),
            argv=("/usr/local/bin/python", "-V"),
            inputs=PreparedInputs(snapshot_id=UUID(int=2), files=(
                PreparedFile(path="source.py", size=1, sha256="2" * 64),)),
        ),
        artifact=GuardExecutionArtifact(
            sha256="3" * 64, size=4096, platform="linux/arm64", workload_filter_sha256="4" * 64,
        ),
        startup_wait_ns=100, clock_rate_floor_ppm=900000, deadline_margin_ns=20,
    )
    return GuardV2Session(manifest, GuardV2Challenge(
        nonce=UUID(int=1), manifest_digest=manifest.fingerprint,
        issued_ns=1000, startup_deadline_ns=1100, max_runtime_ns=5000000000,
    ))


def test_authorization_is_not_dispatch_and_exit124_is_not_a_timeout(
    session: GuardV2Session,
) -> None:
    assert str(session.state) == "parked"
    authorization = session.prepare_authorization(host_now_ns=500, host_deadline_ns=1500)
    assert authorization.deadline_ns == 1882  # (1000 - 20) * 90% + guest anchor1000.
    assert str(session.state) == "authorization_prepared"
    assert not session.runtime_verified and not session.launch_authority_issued
    dispatch = GuardV2Dispatch(nonce=authorization.nonce,
                              manifest_digest=authorization.manifest_digest,
                              sequence=1, observed_ns=1050)
    assert session.feed_guard_stderr(encode_guard_v2_frame(dispatch)) == ()
    assert str(session.state) == "dispatch_candidate"
    terminal = GuardV2Terminal(
        nonce=authorization.nonce, manifest_digest=authorization.manifest_digest,
        sequence=2, observed_ns=1100, cause="workload_exit", exit_code=124,
        stderr_eof=True, stderr_dropped=False,
    )
    assert session.feed_guard_stderr(encode_guard_v2_frame(terminal)) == ()
    assert session.finish() == terminal
    assert session.state == "complete_candidate"
    assert session.terminal_candidate is not None
    assert session.terminal_candidate.cause == "workload_exit"
    assert not session.runtime_verified


def test_original_profile_budget_is_capped_before_rate_scaling(session: GuardV2Session) -> None:
    authorization = session.prepare_authorization(host_now_ns=0, host_deadline_ns=10000000000)
    # Five seconds maximum, minus20ns, then90%, anchored at guest1000ns.
    assert authorization.deadline_ns == 4500000982


@pytest.mark.parametrize("condition", ["digest", "startup", "runtime"])
def test_foreign_challenge_cannot_initialize_receiver(
    session: GuardV2Session, condition: str,
) -> None:
    fields = session.challenge.model_dump()
    fields[{"digest": "manifest_digest", "startup": "startup_deadline_ns",
            "runtime": "max_runtime_ns"}[condition]] = "9" * 64 if condition == "digest" else 1200
    with pytest.raises(GuardV2ProtocolFault):
        GuardV2Session(session.manifest, GuardV2Challenge.model_validate(fields))


def test_bound_manifest_and_challenge_are_read_only(session: GuardV2Session) -> None:
    for name in ("manifest", "challenge"):
        with pytest.raises(AttributeError):
            setattr(session, name, getattr(session, name))


def dispatch(session: GuardV2Session) -> None:
    session.prepare_authorization(host_now_ns=500, host_deadline_ns=1500)
    session.feed_guard_stderr(encode_guard_v2_frame(GuardV2Dispatch(
        nonce=session.challenge.nonce, manifest_digest=session.manifest.fingerprint,
        sequence=1, observed_ns=1050,
    )))


def terminal(session: GuardV2Session, **changes: object) -> GuardV2Terminal:
    return GuardV2Terminal.model_validate(dict(
        nonce=session.challenge.nonce, manifest_digest=session.manifest.fingerprint,
        sequence=2, observed_ns=1100, cause="workload_exit", exit_code=124,
        stderr_eof=True, stderr_dropped=False,
    ) | changes)


@pytest.mark.parametrize("separate_call", [False, True])
def test_even_one_byte_after_terminal_permanently_invalidates_candidate(
    session: GuardV2Session, separate_call: bool,
) -> None:
    dispatch(session)
    wire = encode_guard_v2_frame(terminal(session))
    with pytest.raises(GuardV2ProtocolFault):
        if separate_call:
            session.feed_guard_stderr(wire)
            session.feed_guard_stderr(b"x")
        else:
            session.feed_guard_stderr(wire + b"x")
    assert session.state == "unknown"
    assert session.terminal_candidate is None


def test_stdout_and_nested_stderr_protocol_bytes_do_not_become_terminal_evidence(
    session: GuardV2Session,
) -> None:
    dispatch(session)
    fake = encode_guard_v2_frame(terminal(session))
    assert session.accept_stdout(fake) == fake
    assert session.state == "dispatch_candidate"
    data = GuardV2Stderr(
        nonce=session.challenge.nonce, manifest_digest=session.manifest.fingerprint,
        sequence=2, data=b"untrusted" + fake,
    )
    wire = encode_guard_v2_frame(data)
    assert session.feed_guard_stderr(wire[:70]) == ()
    assert session.feed_guard_stderr(wire[70:]) == (b"untrusted" + fake,)
    assert session.terminal_candidate is None
    session.feed_guard_stderr(encode_guard_v2_frame(terminal(session, sequence=3)))
    assert session.finish().cause == "workload_exit"


@pytest.mark.parametrize("condition", ["nonce", "digest", "duplicate", "skip", "control"])
def test_foreign_replayed_or_wrong_direction_frames_do_not_reopen_session(
    session: GuardV2Session, condition: str,
) -> None:
    dispatch(session)
    fields: dict[str, object] = dict(nonce=session.challenge.nonce,
                                    manifest_digest=session.manifest.fingerprint,
                                    sequence=2, data=b"stderr")
    if condition == "nonce":
        fields["nonce"] = UUID(int=99)
    elif condition == "digest":
        fields["manifest_digest"] = "9" * 64
    elif condition == "duplicate":
        fields["sequence"] = 1
    elif condition == "skip":
        fields["sequence"] = 3
    wire = encode_guard_v2_frame(GuardV2Stderr.model_validate(fields))
    if condition == "control":
        from jeff.application.execution import GuardV2Abort

        wire = encode_guard_v2_frame(GuardV2Abort(
            nonce=session.challenge.nonce, manifest_digest=session.manifest.fingerprint,
        ))
    with pytest.raises(GuardV2ProtocolFault):
        session.feed_guard_stderr(wire)
    assert session.state == "unknown"
    with pytest.raises(GuardV2ProtocolFault):
        session.prepare_authorization(host_now_ns=500, host_deadline_ns=1500)


@pytest.mark.parametrize("condition", ["missing", "partial", "loss"])
def test_no_terminal_delivery_guarantee_or_false_complete_output(
    session: GuardV2Session, condition: str,
) -> None:
    dispatch(session)
    if condition == "partial":
        session.feed_guard_stderr(encode_guard_v2_frame(terminal(session))[:-1])
    elif condition == "loss":
        session.close()
    with pytest.raises(GuardV2ProtocolFault):
        session.finish()
    assert session.state == "unknown" and session.terminal_candidate is None


def test_maximum_data_frame_is_incremental_and_buffer_bounded(session: GuardV2Session) -> None:
    dispatch(session)
    frame = GuardV2Stderr(
        nonce=session.challenge.nonce, manifest_digest=session.manifest.fingerprint,
        sequence=2, data=b"x" * 4096,
    )
    wire = encode_guard_v2_frame(frame)
    assert len(wire) == 4173
    assert session.feed_guard_stderr(wire[:4096]) == ()
    assert session.buffered_bytes == 4096
    assert session.feed_guard_stderr(wire[4096:]) == (b"x" * 4096,)
    assert session.buffered_bytes == 0
    with pytest.raises(GuardV2ProtocolFault):
        session.feed_guard_stderr(b"x" * 4097)
    assert session.state == "unknown"


@pytest.mark.parametrize("time_ns", [1882, 1883])
def test_deadline_wins_normal_reap_race(session: GuardV2Session, time_ns: int) -> None:
    dispatch(session)
    with pytest.raises(GuardV2ProtocolFault):
        session.feed_guard_stderr(encode_guard_v2_frame(terminal(session, observed_ns=time_ns)))
    assert session.state == "unknown"


@pytest.mark.parametrize("cause", ["deadline_expired", "guard_fault"])
def test_deadline_and_clock_fault_are_distinct_even_without_dispatch(
    session: GuardV2Session, cause: str,
) -> None:
    session.prepare_authorization(host_now_ns=500, host_deadline_ns=1500)
    frame = terminal(session, sequence=1, cause=cause, exit_code=None,
                     fault="clock_regression" if cause == "guard_fault" else None,
                     observed_ns=900 if cause == "guard_fault" else 1882,
                     stderr_eof=False, stderr_dropped=True)
    session.feed_guard_stderr(encode_guard_v2_frame(frame))
    assert session.finish().cause == cause
    assert not frame.logical_stderr_complete
    assert not session.runtime_verified


@pytest.mark.parametrize("fault, started", [("startup_refused", True), ("exec_failed", False)])
def test_fault_detail_cannot_contradict_dispatch_history(
    session: GuardV2Session, fault: str, started: bool,
) -> None:
    if started:
        dispatch(session)
    else:
        session.prepare_authorization(host_now_ns=500, host_deadline_ns=1500)
    frame = terminal(session, sequence=2 if started else 1, cause="guard_fault",
                     exit_code=None, fault=fault)
    with pytest.raises(GuardV2ProtocolFault):
        session.feed_guard_stderr(encode_guard_v2_frame(frame))
    assert session.state == "unknown"


@pytest.mark.parametrize("condition", ["empty_budget", "duplicate"])
def test_candidate_budget_failure_never_restores_permission(
    session: GuardV2Session, condition: str,
) -> None:
    if condition == "duplicate":
        session.prepare_authorization(host_now_ns=500, host_deadline_ns=1500)
    with pytest.raises(GuardV2ProtocolFault):
        session.prepare_authorization(host_now_ns=500,
                                      host_deadline_ns=500 if condition == "empty_budget" else 1500)
    assert session.state == "unknown"
    with pytest.raises(GuardV2ProtocolFault):
        session.prepare_authorization(host_now_ns=500, host_deadline_ns=1500)
