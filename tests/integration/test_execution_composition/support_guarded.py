"""Guarded fake transport at the existing controller seam; no subprocess or Docker."""

from collections.abc import Callable
from typing import cast
from uuid import UUID

from jeff.application.execution import (
    DockerBackendFault,
    DockerLaunchIntent,
    DockerOutputSink,
    DockerOwnedContainer,
    ExecutionStopSignal,
    GuardedExecutionManifest,
    GuardExecutionArtifact,
    GuardExecutionPolicy,
    GuardV2Terminal,
    guard_arguments,
)
from jeff.infrastructure.runtime_execution.guard_artifacts import resource_path

from .support_docker import Docker


def policy() -> GuardExecutionPolicy:
    return GuardExecutionPolicy(
        artifacts=(
            GuardExecutionArtifact(
                sha256="7d86843d91d42ffa3bb53d1823d33fbde9397e5d473671b083560e6ba559406c",
                size=9888,
                platform="linux/arm64",
                workload_filter_sha256="cf66160cb6e8b0c792af7ebe6bbb9d41ea5732af1f6ef01af203d6e576373849",
            ),
        ),
        startup_wait_ns=2000000000,
        clock_rate_floor_ppm=900000,
        deadline_margin_ns=100000000,
    )


class GuardedDocker(Docker):
    def create(self, intent: DockerLaunchIntent, *, deadline: float) -> str:
        cid = super().create(intent, deadline=deadline)
        action = intent.record.manifest
        assert isinstance(action, GuardedExecutionManifest)
        config = cast(dict[str, object], self.raw["Config"])
        config.update(
            Entrypoint=[action.artifact.container_executable],
            Cmd=list(guard_arguments(action)),
            OpenStdin=True,
            AttachStdin=True,
            AttachStdout=True,
            AttachStderr=True,
            StdinOnce=True,
        )
        cast(dict[str, object], self.raw["HostConfig"])["Init"] = False
        self.raw.update(
            Path=action.artifact.container_executable, Args=list(guard_arguments(action))
        )
        cast(list[dict[str, object]], self.raw["Mounts"]).append(
            dict(
                Type="bind",
                Destination=action.artifact.container_executable,
                Source=str(resource_path(action.artifact)),
                RW=False,
                Propagation="rprivate",
            )
        )
        return cid

    def inspect(self, endpoint: str, container_id: str, *, deadline: float) -> dict[str, object]:
        raw = super().inspect(endpoint, container_id, deadline=deadline)
        cast(dict[str, object], raw["State"]).update(
            Pid=0 if self.state != "running" else 2, OOMKilled=False, Error=""
        )
        return raw

    def attach_guarded(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        stop: ExecutionStopSignal | None,
        observe_running: Callable[[], None] | None,
        *,
        deadline: float,
    ) -> tuple[int, GuardV2Terminal]:
        self.calls.append("attach_guarded")
        self.state = "running"
        if self.failure in ("protocol", "clock_fault"):
            raise DockerBackendFault("docker_output_failed")
        if self.cancel_mode:
            assert stop is not None and observe_running is not None
            self.attach_cancellable(
                handle.intent.target.endpoint,
                handle.container_id,
                sink,
                stop,
                observe_running,
                deadline=deadline,
            )
        sink.accept("stdout", self.output)
        sink.finish("stdout")
        deadline_case = self.failure == "deadline"
        if not deadline_case:
            sink.finish("stderr")
        self.state = "exited"
        if deadline_case:
            self.exit_code = 124
        identity = dict(
            nonce=UUID(int=1),
            manifest_digest=handle.intent.record.manifest_digest,
            sequence=2,
            observed_ns=100,
            stderr_eof=not deadline_case,
            stderr_dropped=False,
        )
        terminal = GuardV2Terminal.model_validate(
            identity
            | (
                dict(cause="deadline_expired")
                if deadline_case
                else dict(cause="workload_exit", exit_code=self.exit_code)
            )
        )
        return self.exit_code, terminal

    def release_guarded(self, handle: DockerOwnedContainer, *, deadline: float) -> bool:
        self.calls.append("release_guarded")
        return True
