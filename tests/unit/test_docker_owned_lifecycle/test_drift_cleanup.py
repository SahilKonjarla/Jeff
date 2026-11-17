"""Fail-closed input/ownership drift and bounded exact-ID cleanup without remove replay."""

import time

from .support_controller import setup_controller
from .support_ports import Inputs, Sink


def test_configuration_input_and_ownership_drift_fail_closed() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    config = backend.raw["Config"]
    assert isinstance(config, dict)
    config["User"] = "0"
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.outcome_unknown and outcome.cleanup_verified
    assert backend.calls == [
        "create",
        "remove",
    ]  # Unsafe configuration does not prevent owned cleanup.
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    changed = Inputs()
    changed.changed = True
    controller.inputs = changed
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.cleanup_verified and backend.calls == ["create", "remove"]
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    config = backend.raw["Config"]
    assert isinstance(config, dict)
    actual_labels = config["Labels"]
    assert isinstance(actual_labels, dict)
    actual_labels["jeff.nonce"] = "f" * 32
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.outcome_unknown and not outcome.cleanup_verified and backend.calls == ["create"]

    for running in (True, None, "false"):
        controller, backend, journal, claim, declaration = setup_controller()
        handle = controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
        backend.state_override = {"Status": "created", "Running": running}
        outcome = controller.run(
            handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
        )
        assert outcome.outcome_unknown and "attach" not in backend.calls

    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    sink = Sink()
    sink.retention_limit = 2097152
    outcome = controller.run(
        handle, sink, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.outcome_unknown and outcome.cleanup_verified and "attach" not in backend.calls


def test_timeout_has_bounded_exact_id_cleanup_and_remove_is_not_replayed() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.timeout_attach = True
    backend.stop_leaves_running = True
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.timed_out and outcome.outcome_unknown and outcome.cleanup_verified
    assert backend.calls == ["create", "attach", "stop", "kill", "remove"]
    assert controller.terminate_remove_owned(handle, deadline=time.monotonic() + 5)
    assert backend.calls.count("remove") == 1
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.remove_reply_lost = True
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.exit_code == 7 and outcome.outcome_unknown and not outcome.cleanup_verified
    assert controller.terminate_remove_owned(handle, deadline=time.monotonic() + 5)
    assert backend.calls.count("remove") == 1  # Reconcile absence, never repeat rm after ACK loss.
