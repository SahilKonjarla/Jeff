"""Bounded Compose label observations, never evidence of mutation authority."""

import re
from dataclasses import dataclass
from pathlib import Path

from jeff.application.execution import DockerBackendFault

from .metadata import _json
from .queries import _LIMIT
from .transport import _DockerCLI

COMPOSE_STATUS_FORMAT = (
    '[{{json .ID}},{{json (.Label "com.docker.compose.project")}},'
    '{{json (.Label "com.docker.compose.service")}},'
    '{{json (.Label "com.docker.compose.project.working_dir")}},'
    '{{json (.Label "com.docker.compose.project.config_files")}},'
    '{{json (.Label "com.docker.compose.oneoff")}},{{json .State}}]'
)
_STATES = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}


@dataclass(frozen=True)
class ComposeStatus:
    states: tuple[str, ...]
    ambiguous: bool


def valid_compose_name(name: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name) is not None


class ComposeInspectionCLI(_DockerCLI):
    """Reuse pinned capture without extending the shared execution transport contract."""

    def compose_status(
        self, *, endpoint: str, project: str, service: str, root: Path, deadline: float
    ) -> ComposeStatus:
        """Observe a declared single-service fixture; never invoke the Compose plugin."""
        if not valid_compose_name(project) or not valid_compose_name(service):
            raise DockerBackendFault("docker_unsafe_configuration")
        data = self._control(
            (
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                COMPOSE_STATUS_FORMAT,
            ),
            endpoint=endpoint,
            deadline=deadline,
        )
        return parse_compose_status(data, project=project, service=service, root=root)


def parse_compose_status(
    data: bytes, *, project: str, service: str, root: Path
) -> ComposeStatus:
    """Only return allowlisted state; never expose raw labels or container IDs."""
    if len(data) > _LIMIT:
        raise DockerBackendFault("docker_output_limit")
    rows = data.splitlines()
    if len(rows) > 64:
        raise DockerBackendFault("docker_output_limit")
    seen: set[str] = set()
    states: list[str] = []
    ambiguous = False
    for line in rows:
        try:
            row = _json(line)
            if not isinstance(row, list) or len(row) != 7:
                raise ValueError
            if not all(isinstance(field, str) for field in row):
                raise ValueError
            cid, name, member, directory, config, oneoff, state = row
            if (
                re.fullmatch(r"[0-9a-f]{64}", cid) is None
                or cid in seen
                or state not in _STATES
            ):
                raise ValueError
        except (ValueError, RecursionError, DockerBackendFault):
            raise DockerBackendFault("docker_protocol_error") from None
        seen.add(cid)
        if (name, member, directory, config) != (
            project,
            service,
            str(root),
            str(root / "compose.yaml"),
        ):
            ambiguous = True
        elif oneoff not in ("True", "False"):
            ambiguous = True
        elif oneoff == "False":
            states.append(state)
    return ComposeStatus(tuple(sorted(states)), ambiguous)
