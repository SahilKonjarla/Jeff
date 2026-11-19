"""Fixed lifecycle commands and captured attachment; controller owns mutation authority."""

import math
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from jeff.application.execution import (
    DockerBackendFault,
    DockerLaunchIntent,
    DockerOutputSink,
    DockerOwnedContainer,
    ExecutionStopSignal,
    GuardV2Terminal,
)

from .guarded import GuardAttachment
from .metadata import _Engine, _json
from .transport import _DockerCLI


class DockerLifecycleCLI(_DockerCLI):
    """Fixed internal operations. A controller must supply live ownership/intent proofs.

    Not composed into bootstrap/RPC/CLI until real journal/input/output adapters exist.
    """

    def __init__(
        self, binary: Path, config: Path, home: Path, *, deadline: float | None = None
    ) -> None:
        super().__init__(binary, config, home, deadline=deadline)
        self._guarded: dict[int, GuardAttachment] = {}

    def attach_guarded(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        stop: ExecutionStopSignal | None,
        observe_running: Callable[[], None] | None,
        *,
        deadline: float,
    ) -> tuple[int, GuardV2Terminal]:
        from .guarded import attach

        if id(handle) in self._guarded:
            raise DockerBackendFault("docker_authority_required")
        capture = GuardAttachment(handle)
        self._guarded[id(handle)] = capture
        return attach(self, capture, sink, stop, observe_running, deadline=deadline)

    def release_guarded(self, handle: DockerOwnedContainer, *, deadline: float) -> bool:
        capture = self._guarded.get(id(handle))
        if capture is None:
            return True
        if capture.handle is not handle or not capture.release(deadline):
            return False
        del self._guarded[id(handle)]
        return True

    @staticmethod
    def _id(container_id: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise DockerBackendFault("docker_ownership_unconfirmed")
        return container_id

    def engine_id(self, endpoint: str, *, deadline: float) -> str:
        return _Engine.model_validate(
            _json(self.read("engine.info", endpoint=endpoint, image_id=None, deadline=deadline))
        ).ID

    def create(self, intent: DockerLaunchIntent, *, deadline: float) -> str:
        from jeff.infrastructure.runtime_execution import create_arguments

        if intent.target.binary != self.identity:
            raise DockerBackendFault("docker_target_mismatch")
        observed: str | None = None

        def capture_id(reply: bytes) -> None:
            nonlocal observed
            try:
                observed = self._id(reply.strip().decode("ascii"))
            except (UnicodeError, ValueError, DockerBackendFault):
                raise DockerBackendFault("docker_mutation_unconfirmed") from None

        try:
            self._control(
                create_arguments(intent),
                endpoint=intent.target.endpoint,
                deadline=deadline,
                observe_reply=capture_id,
            )
        except DockerBackendFault as error:
            raise DockerBackendFault(
                "docker_mutation_unconfirmed",
                diagnostics=error.diagnostics,
                observed_container_id=observed,
            ) from None
        assert observed is not None
        return observed

    def inspect(self, endpoint: str, container_id: str, *, deadline: float) -> dict[str, object]:
        template = (
            '{"Id":{{json .Id}},"Image":{{json .Image}},"Created":{{json .Created}},'
            '"Path":{{json .Path}},"Args":{{json .Args}},"Config":{{json .Config}},'
            '"HostConfig":{{json .HostConfig}},"Mounts":{{json .Mounts}},"State":{{json .State}}}'
        )
        try:
            value = _json(
                self._control(
                    ("container", "inspect", "--format", template, "--", self._id(container_id)),
                    endpoint=endpoint,
                    deadline=deadline,
                )
            )
            if not isinstance(value, dict):
                raise ValueError("Container metadata must be an object.")
            return cast(dict[str, object], value)
        except (ValueError, TypeError, RecursionError):
            raise DockerBackendFault("docker_protocol_error") from None

    def stop(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        # Docker stop may escalate internally after its one-second grace period.
        self._control(
            ("container", "stop", "--timeout", "1", "--", self._id(container_id)),
            endpoint=endpoint,
            deadline=deadline,
        )

    def stop_gracefully(
        self, endpoint: str, container_id: str, *, grace_seconds: int, deadline: float
    ) -> None:
        if type(grace_seconds) is not int or not 0 <= grace_seconds <= 5:
            raise DockerBackendFault("docker_unsafe_configuration")
        # Docker may escalate internally at the end of this remaining grace budget.
        self._control(
            ("container", "stop", "--timeout", str(grace_seconds), "--", self._id(container_id)),
            endpoint=endpoint,
            deadline=deadline,
        )

    def kill(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        self._control(
            ("container", "kill", "--signal", "KILL", "--", self._id(container_id)),
            endpoint=endpoint,
            deadline=deadline,
        )

    def remove(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        self._control(
            ("container", "rm", "--", self._id(container_id)), endpoint=endpoint, deadline=deadline
        )  # Never force/volume/broad cleanup.

    def absent(self, endpoint: str, container_id: str, *, deadline: float) -> bool:
        identifier = self._id(container_id)
        result = self._control(
            ("container", "ls", "--all", "--quiet", "--no-trunc", "--filter", "id=" + identifier),
            endpoint=endpoint,
            deadline=deadline,
        ).strip()
        if result not in (b"", identifier.encode("ascii")):
            raise DockerBackendFault("docker_protocol_error")
        return not result

    def attach(
        self, endpoint: str, container_id: str, sink: DockerOutputSink, *, deadline: float
    ) -> int:
        return self._attach(endpoint, container_id, sink, deadline=deadline)

    def attach_cancellable(
        self,
        endpoint: str,
        container_id: str,
        sink: DockerOutputSink,
        stop: ExecutionStopSignal,
        observe_running: Callable[[], None],
        *,
        deadline: float,
    ) -> int:
        return self._attach(
            endpoint,
            container_id,
            sink,
            deadline=deadline,
            stop=stop,
            observe_running=observe_running,
        )

    def _attach(
        self,
        endpoint: str,
        container_id: str,
        sink: DockerOutputSink,
        *,
        deadline: float,
        stop: ExecutionStopSignal | None = None,
        observe_running: Callable[[], None] | None = None,
    ) -> int:
        self.check()
        socket_identity = self._socket(endpoint)
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise DockerBackendFault("docker_timeout")
        argv = [
            str(self.binary),
            "--config",
            str(self.config),
            "--host",
            endpoint,
            "container",
            "start",
            "--attach",
            "--",
            self._id(container_id),
        ]
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        next_observation = 0.0
        try:
            if stop is not None:
                stop.check()
            process = subprocess.Popen(
                argv,
                cwd=self.config,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            assert process.stdout is not None and process.stderr is not None
            for pipe, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, label)
            while selector.get_map() or process.poll() is None:
                if stop is not None:
                    stop.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerBackendFault("docker_timeout")
                if observe_running is not None and time.monotonic() >= next_observation:
                    observe_running()  # Same owner, bounded exact-identity read-only probe.
                    next_observation = time.monotonic() + 0.5
                for key, _ in selector.select(min(0.05, remaining)):
                    try:
                        chunk = os.read(key.fd, 16384)
                    except BlockingIOError:
                        continue
                    stream: Literal["stdout", "stderr"] = key.data
                    if chunk:
                        sink.accept(stream, chunk)  # Trusted bounded in-memory redactor only.
                    else:
                        selector.unregister(key.fileobj)
                        sink.finish(stream)
            code = process.wait(timeout=1)
            self.check()
            if self._socket(endpoint) != socket_identity:
                raise DockerBackendFault("docker_target_mismatch")
            return code  # CLI status is not the container's authoritative exit observation.
        except DockerBackendFault:
            raise
        except OSError:
            raise DockerBackendFault("docker_unavailable") from None
        except Exception:
            raise DockerBackendFault("docker_output_failed") from None
        finally:
            selector.close()
            if process is not None:
                try:
                    if process.poll() is None:
                        # start --attach may proxy SIGTERM. Kill only the captured CLI;
                        # the controller separately revalidates ownership before job stop/kill.
                        process.kill()
                        process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    raise DockerBackendFault("docker_cleanup_unconfirmed") from None
                finally:
                    for pipe_to_close in (process.stdout, process.stderr):
                        if pipe_to_close is not None:
                            pipe_to_close.close()
