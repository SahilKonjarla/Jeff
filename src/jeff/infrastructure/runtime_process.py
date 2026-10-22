"""Bounded installed-process control. No public CLI wiring, PID/name kill or replay."""

import os
import select
import subprocess
import sys
import time
from contextlib import closing
from uuid import UUID, uuid4

from jeff.application.projects import Failure
from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    ControlFault,
    ControlObservation,
    ControlOperation,
    ControlSuccess,
    RuntimeLayout,
    control_failure,
)
from jeff.domain.runtime import RuntimeState
from jeff.infrastructure.runtime_ipc import ControlClient
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore


def child_environment(layout: RuntimeLayout) -> dict[str, str]:
    """Deliberate environment: no inherited credentials, Python paths or Git verbosity."""
    return {
        "PATH": os.defpath,
        "HOME": str(layout.data_dir),
        "TMPDIR": str(layout.runtime_dir),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }


def _stopped() -> ControlSuccess:
    return ControlSuccess(data=ControlObservation(state=RuntimeState.STOPPED))


class RuntimeController:
    def __init__(self, layout: RuntimeLayout) -> None:
        self.layout = layout
        self._child: subprocess.Popen[bytes] | None = None

    def _reap(self) -> None:
        if self._child is not None:
            self._child.poll()

    def _ready(
        self,
        store: RuntimeStateStore,
        deadline: float,
        operation: ControlOperation = "hello",
    ) -> ControlObservation:
        observation = ControlClient(self.layout, store.read_credential()).request(
            operation, deadline=deadline
        )
        if observation.state != RuntimeState.RUNNING or not observation.control_ready:
            raise ControlFault("runtime_ownership_unconfirmed")
        probe = store.acquire(create=False)
        if probe is not None:
            probe.close()
            raise ControlFault("runtime_ownership_unconfirmed")
        return observation

    def start(self) -> ControlSuccess | Failure:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.start_seconds
        ready_deadline = deadline - 1  # reserve bounded cleanup time for our own failed child
        owner: Ownership | None = None
        spawned: subprocess.Popen[bytes] | None = None
        expected: UUID | None = None
        try:
            self._reap()
            # Concurrent legitimate preparation can invalidate a read-only snapshot
            # (e.g. ancestor link counts). Retry observation, never bypass validation.
            while True:
                try:
                    store = RuntimeStateStore(self.layout)
                    break
                except ControlFault as error:
                    if (
                        error.code != "runtime_inspection_failed"
                        or time.monotonic() >= ready_deadline
                    ):
                        raise
                    time.sleep(0.02)
            owner = store.acquire(create=True)
            if owner is not None:
                owner.require_no_endpoint()
                store.credential(owner)
                expected = uuid4()
                spawned = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-m",
                        "jeff.bootstrap.runtime",
                        "--lock-fd",
                        str(owner.fd),
                        "--instance-id",
                        str(expected),
                    ],
                    pass_fds=(owner.fd,),
                    start_new_session=True,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd="/",
                    env=child_environment(self.layout),
                )
                self._child = spawned
                # close only; LOCK_UN would also unlock the child's inherited description.
                owner.close()
                owner = None
            while time.monotonic() < ready_deadline:
                if spawned is not None and spawned.poll() is not None:
                    raise ControlFault("runtime_start_failed")
                try:
                    observation = self._ready(store, ready_deadline)
                    if expected is not None and (
                        observation.identity is None
                        or observation.identity.instance_id != expected
                        or spawned is None
                        or observation.identity.pid != spawned.pid
                    ):
                        raise ControlFault("runtime_ownership_unconfirmed")
                    return ControlSuccess(data=observation)
                except ControlFault as error:
                    # Only idempotent readiness reads are retried, never an action.
                    if error.code not in (
                        "runtime_unreachable",
                        "runtime_credentials_invalid",
                        "runtime_absent",
                    ):
                        raise
                    time.sleep(min(0.02, max(0, ready_deadline - time.monotonic())))
            raise ControlFault("runtime_start_timeout")
        except (ControlFault, OSError) as error:
            # Genuine Popen ownership, NOT a stored/native-peer PID used as kill authority.
            if spawned is not None and spawned.poll() is None:
                try:
                    spawned.terminate()
                    try:
                        spawned.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        spawned.kill()
                        spawned.wait(timeout=max(0, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    # Keep the genuine handle for later poll/reconciliation. A deadline
                    # cannot justify claiming exit or silently launching a replacement.
                    return control_failure("runtime_ownership_unconfirmed")
            return control_failure(
                error.code if isinstance(error, ControlFault) else "runtime_start_failed"
            )
        finally:
            if owner is not None:
                owner.close()

    def status(self) -> ControlSuccess | Failure:
        try:
            self._reap()
            store = RuntimeStateStore(self.layout)
            try:
                owner = store.acquire(create=False)
            except ControlFault as error:
                if error.code == "runtime_absent":
                    return _stopped()
                raise
            if owner is not None:
                with owner:
                    owner.require_no_endpoint()
                return _stopped()
            return ControlSuccess(
                data=self._ready(
                    store,
                    time.monotonic() + DEFAULT_RUNTIME_LIMITS.request_seconds,
                    operation="status",
                )
            )
        except ControlFault as error:
            return control_failure(error.code)

    def stop(self) -> ControlSuccess | Failure:
        deadline = time.monotonic() + DEFAULT_RUNTIME_LIMITS.stop_seconds
        current = self.status()
        if isinstance(current, Failure) or current.data.state == RuntimeState.STOPPED:
            return current
        identity = current.data.identity
        if identity is None:
            return control_failure("runtime_stop_unconfirmed")
        try:
            store = RuntimeStateStore(self.layout)
            client = ControlClient(self.layout, store.read_credential())
            with client.connect(deadline) as connection:
                if connection.peer_pid != identity.pid:
                    raise ControlFault("runtime_ownership_unconfirmed")
                # Native exit event, registered against an authenticated current peer before
                # shutdown; never signal a PID or confuse a recycled identifier with exit.
                with closing(select.kqueue()) as queue:
                    queue.control(
                        [
                            select.kevent(
                                connection.peer_pid,
                                filter=select.KQ_FILTER_PROC,
                                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                                fflags=select.KQ_NOTE_EXIT,
                            )
                        ],
                        0,
                        0,
                    )
                    try:
                        connection.request("stop", identity.instance_id)
                    except ControlFault:
                        pass  # do not resend; reconcile only through native exit + ownership
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not queue.control([], 1, remaining):
                        raise ControlFault("runtime_stop_unconfirmed")
            remaining = deadline - time.monotonic()
            if self._child is not None and self._child.pid == identity.pid:
                self._child.wait(timeout=max(0.01, remaining))
            owner = store.acquire(create=False)
            if owner is None:
                raise ControlFault("runtime_stop_unconfirmed")
            with owner:
                owner.require_no_endpoint()
            return _stopped()
        except (ControlFault, OSError, subprocess.TimeoutExpired):
            return control_failure("runtime_stop_unconfirmed")
