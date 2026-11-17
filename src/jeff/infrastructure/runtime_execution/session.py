"""Controller-local live handle, mutation attempts and one-shot completion state."""

from dataclasses import dataclass

from jeff.application.execution import DockerJobOutcome, DockerOutputSink, DockerOwnedContainer


@dataclass
class _Session:
    handle: DockerOwnedContainer
    start_attempted: bool = False
    start_recorded: bool = False
    exit_code: int | None = None
    removed: bool = False
    stop_attempted: bool = False
    kill_attempted: bool = False
    remove_attempted: bool = False
    sink: DockerOutputSink | None = None
    outcome: DockerJobOutcome | None = None
    completion_attempted: bool = False
