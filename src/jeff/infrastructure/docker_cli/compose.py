"""Bounded Compose label observations, never evidence of mutation authority."""

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from jeff.application.compose_status import (
    COMPOSE_LOG_LIMIT,
    COMPOSE_LOG_TAIL,
    ComposeHealth,
    ComposeLogsData,
    PublishedPort,
    PublishedPorts,
)
from jeff.application.execution import DockerBackendFault
from jeff.infrastructure.runtime_output import GuardedOutputCollector

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
_DETAILS_FORMAT = (
    '[{{json .Id}},{{json (index .Config.Labels "com.docker.compose.project")}},'
    '{{json (index .Config.Labels "com.docker.compose.service")}},'
    '{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
    '{{json (index .Config.Labels "com.docker.compose.project.config_files")}},'
    '{{json (index .Config.Labels "com.docker.compose.oneoff")}},{{json .State.Status}},'
    '{{if .State.Health}}{{json .State.Health.Status}}'
    '{{else if .Config.Healthcheck}}{{if .Config.Healthcheck.Test}}'
    '{{if eq (index .Config.Healthcheck.Test 0) "NONE"}}"not_configured"'
    '{{else}}"unknown"{{end}}{{else}}"not_configured"{{end}}'
    '{{else}}"not_configured"{{end}},'
    '{{if .NetworkSettings}}{{json .NetworkSettings.Ports}}{{else}}false{{end}}]'
)
_STATES = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}


@dataclass(frozen=True)
class ComposeStatus:
    states: tuple[str, ...]
    ambiguous: bool
    health: tuple[ComposeHealth, ...] = ()
    published_ports: tuple[PublishedPorts | None, ...] = ()


def valid_compose_name(name: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name) is not None


class ComposeInspectionCLI(_DockerCLI):
    """Reuse pinned capture without extending the shared execution transport contract."""

    def compose_status(
        self, *, endpoint: str, project: str, service: str, root: Path, deadline: float
    ) -> ComposeStatus:
        """Observe a declared single-service fixture; never invoke the Compose plugin."""
        matches, ambiguous = self._matching_rows(
            endpoint=endpoint, project=project, service=service, root=root, deadline=deadline
        )
        matches.sort(key=lambda row: (row[-1], row[0]))
        details = [self._details(row, endpoint=endpoint, deadline=deadline) for row in matches]
        return ComposeStatus(
            tuple(row[-1] for row in matches),
            ambiguous,
            tuple(health for health, _ in details),
            tuple(ports for _, ports in details),
        )

    def _matching_rows(
        self, *, endpoint: str, project: str, service: str, root: Path, deadline: float
    ) -> tuple[list[tuple[str, ...]], bool]:
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
        return _match_rows(data, project=project, service=service, root=root)

    def compose_logs(
        self, *, endpoint: str, project: str, service: str, root: Path, deadline: float
    ) -> ComposeLogsData:
        """Read one finite tail only after corroborating the exact fixture container."""
        self.check()
        socket_identity = self._socket(endpoint)
        matches, ambiguous = self._matching_rows(
            endpoint=endpoint, project=project, service=service, root=root, deadline=deadline
        )
        self.check()
        if ambiguous or len(matches) != 1 or self._socket(endpoint) != socket_identity:
            raise DockerBackendFault("docker_target_mismatch")
        capture = GuardedOutputCollector(COMPOSE_LOG_LIMIT)
        try:
            self._capture(
                [
                    str(self.binary), "--config", str(self.config), "--host", endpoint,
                    "container", "logs", "--tail", str(COMPOSE_LOG_TAIL), "--", matches[0][0],
                ],
                self._environment(),
                min(deadline, time.monotonic() + 5),
                log_sink=capture.accept,
            )
        except DockerBackendFault as error:
            if error.code == "docker_output_limit":
                return ComposeLogsData(compose_project=project, service=service, withheld="output_limit")
            raise
        self.check()
        if self._socket(endpoint) != socket_identity or time.monotonic() >= deadline:
            raise DockerBackendFault("docker_target_mismatch")
        capture.finish("stdout")
        capture.finish("stderr")
        result = capture.seal()
        if result.guard_reason != "none" or result.truncated:
            return ComposeLogsData(compose_project=project, service=service, withheld="sanitizer")
        return ComposeLogsData(
            compose_project=project, service=service, stdout=result.stdout, stderr=result.stderr
        )

    def _details(
        self, row: tuple[str, ...], *, endpoint: str, deadline: float
    ) -> tuple[ComposeHealth, PublishedPorts | None]:
        if time.monotonic() >= deadline:
            return "unknown", None
        try:
            observed = _json(
                self._control(
                    ("container", "inspect", "--format", _DETAILS_FORMAT, "--", row[0]),
                    endpoint=endpoint,
                    deadline=deadline,
                )
            )
        except (DockerBackendFault, ValueError, RecursionError):
            return "unknown", None
        if not isinstance(observed, list) or len(observed) != 9 or tuple(observed[:7]) != row:
            return "unknown", None
        health: ComposeHealth = "unknown"
        if observed[7] in ("starting", "healthy", "unhealthy", "not_configured"):
            health = cast(ComposeHealth, observed[7])
        return health, _published_ports(observed[8])


def _published_ports(raw: object) -> PublishedPorts | None:
    if raw is None:
        return ()  # Docker has no runtime port binding table.
    if not isinstance(raw, dict) or len(raw) > 64:
        return None
    bindings: list[PublishedPort] = []
    try:
        for target, hosts in raw.items():
            if not isinstance(target, str):
                return None
            match = re.fullmatch(r"([0-9]{1,5})/(tcp|udp|sctp)", target)
            if match is None or not 1 <= int(match[1]) <= 65535:
                return None
            if hosts is None:  # An exposed but unpublished container port.
                continue
            if not isinstance(hosts, list) or len(hosts) > 64 - len(bindings):
                return None
            for host in hosts:
                if not isinstance(host, dict) or host.keys() != {"HostIp", "HostPort"}:
                    return None
                port = host["HostPort"]
                if not isinstance(port, str) or not re.fullmatch(r"[0-9]{1,5}", port):
                    return None
                bindings.append(
                    PublishedPort.model_validate(
                        {
                            "host_ip": host["HostIp"],
                            "host_port": int(port),
                            "container_port": int(match[1]),
                            "protocol": match[2],
                        }
                    )
                )
    except ValueError:
        return None
    return tuple(sorted(bindings, key=lambda b: (b.container_port, b.protocol, b.host_ip, b.host_port)))


def parse_compose_status(
    data: bytes, *, project: str, service: str, root: Path
) -> ComposeStatus:
    """Project the list observation alone, without a separate health observation."""
    matches, ambiguous = _match_rows(data, project=project, service=service, root=root)
    return ComposeStatus(tuple(sorted(row[-1] for row in matches)), ambiguous)


def _match_rows(
    data: bytes, *, project: str, service: str, root: Path
) -> tuple[list[tuple[str, ...]], bool]:
    if len(data) > _LIMIT:
        raise DockerBackendFault("docker_output_limit")
    rows = data.splitlines()
    if len(rows) > 64:
        raise DockerBackendFault("docker_output_limit")
    seen: set[str] = set()
    matches: list[tuple[str, ...]] = []
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
            matches.append(tuple(row))
    return matches, ambiguous
