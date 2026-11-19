"""Original JFG2 attachment. Uncertain captures stay observe-only; never kill/close on failure."""

import os
import selectors
import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from jeff.application.execution import (
    DockerBackendFault,
    DockerOutputSink,
    DockerOwnedContainer,
    ExecutionStopSignal,
    GuardedExecutionManifest,
    GuardV2Challenge,
    GuardV2ProtocolFault,
    GuardV2Session,
    GuardV2Terminal,
    decode_guard_v2_frame,
    encode_guard_v2_frame,
)

if TYPE_CHECKING:
    from .lifecycle import DockerLifecycleCLI


class GuardAttachment:
    def __init__(self, handle: DockerOwnedContainer) -> None:
        self.handle = handle
        self.process: subprocess.Popen[bytes] | None = None
        self.session: GuardV2Session | None = None
        self.header = bytearray()
        self.authorized = False
        self.unknown = False

    def failed(self) -> None:
        self.unknown = True
        if self.session is not None:
            self.session.close()

    def stdout(self, data: bytes, sink: DockerOutputSink, deadline: float) -> None:
        action = self.handle.intent.record.manifest
        assert isinstance(action, GuardedExecutionManifest)
        if self.session is not None:
            if not self.authorized:
                raise GuardV2ProtocolFault("Authorization write not confirmed.")
            sink.accept("stdout", self.session.accept_stdout(data))
            return
        self.header.extend(data)
        if len(self.header) > 77:
            raise GuardV2ProtocolFault("Unexpected output before authorization.")
        if len(self.header) < 77:
            return
        challenge = decode_guard_v2_frame(bytes(self.header))
        if not isinstance(challenge, GuardV2Challenge):
            raise GuardV2ProtocolFault("Expected challenge.")
        self.session = GuardV2Session(action, challenge)
        sample = time.monotonic_ns()
        left = max(0, int((deadline - time.monotonic()) * 1000000000))
        frame = encode_guard_v2_frame(
            self.session.prepare_authorization(host_now_ns=sample, host_deadline_ns=sample + left)
        )
        assert self.process is not None and self.process.stdin is not None
        # Exactly one nonblocking write: partial/error/zero must NEVER be retried.
        count = os.write(self.process.stdin.fileno(), frame)
        if count != len(frame):
            raise GuardV2ProtocolFault("Authorization outcome uncertain.")
        self.authorized = True

    def stderr(self, data: bytes, sink: DockerOutputSink) -> None:
        if not self.authorized or self.session is None:
            raise GuardV2ProtocolFault("Status before authorization.")
        for payload in self.session.feed_guard_stderr(data):
            sink.accept("stderr", payload)  # Opaque, never recursive framing.

    def release(self, deadline: float) -> bool:
        """Caller must already have confirmed original-owned container exit/removal."""
        if self.unknown:
            return False
        process = self.process
        if process is None:
            return True
        try:
            if process.poll() is None:
                process.wait(timeout=max(0, min(1, deadline - time.monotonic())))
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            return True
        except (OSError, subprocess.TimeoutExpired):
            self.failed()
            return False


def attach(
    cli: "DockerLifecycleCLI",
    capture: GuardAttachment,
    sink: DockerOutputSink,
    stop: ExecutionStopSignal | None,
    observe_running: Callable[[], None] | None,
    *,
    deadline: float,
) -> tuple[int, GuardV2Terminal]:
    handle = capture.handle
    cli.check()
    endpoint = handle.intent.target.endpoint
    socket_identity = cli._socket(endpoint)
    selector = selectors.DefaultSelector()
    next_observation = 0.0
    try:
        if time.monotonic() >= deadline:
            raise DockerBackendFault("docker_timeout")
        if stop is not None:
            stop.check()
        argv = [
            str(cli.binary),
            "--config",
            str(cli.config),
            "--host",
            endpoint,
            "container",
            "start",
            "--attach",
            "--interactive",
            "--",
            cli._id(handle.container_id),
        ]
        process = subprocess.Popen(
            argv,
            cwd=cli.config,
            env=cli._environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        capture.process = process  # Genuine original object, retained BEFORE subsequent checks.
        assert (
            process.stdout is not None and process.stderr is not None and process.stdin is not None
        )
        cli.check()
        if cli._socket(endpoint) != socket_identity:
            raise DockerBackendFault("docker_target_mismatch")
        os.set_blocking(process.stdin.fileno(), False)
        for pipe, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, label)
        while selector.get_map() or process.poll() is None:
            if stop is not None:
                stop.check()  # Explicit cancellation is distinct from protocol/clock uncertainty.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DockerBackendFault("docker_timeout")
            if observe_running is not None and time.monotonic() >= next_observation:
                observe_running()
                next_observation = time.monotonic() + 0.5
            stream: Literal["stdout", "stderr"]
            for key, _ in selector.select(min(0.05, remaining)):
                try:
                    data = os.read(key.fd, 4096)
                except BlockingIOError:
                    continue
                stream = key.data
                if data:
                    if stream == "stdout":
                        capture.stdout(data, sink, deadline)
                    else:
                        capture.stderr(data, sink)
                else:
                    selector.unregister(key.fileobj)
                    if stream == "stdout":
                        sink.finish("stdout")
        code = process.wait(timeout=1)
        if capture.session is None:
            raise GuardV2ProtocolFault("Missing challenge/terminal.")
        terminal = capture.session.finish()
        if terminal.cause == "guard_fault":
            raise GuardV2ProtocolFault("Guard fault, not a workload timeout.")
        if terminal.logical_stderr_complete:
            sink.finish("stderr")
        cli.check()
        if cli._socket(endpoint) != socket_identity:
            raise DockerBackendFault("docker_target_mismatch")
        return code, terminal
    except DockerBackendFault as error:
        if error.code != "docker_cancelled":
            capture.failed()
        raise
    except Exception:
        capture.failed()
        raise DockerBackendFault("docker_output_failed") from None
    finally:
        selector.close()  # Does not close pipes, send input or signal the original process.
