"""Read-only Compose projection and fail-closed fixture matching."""

import json
import time
from pathlib import Path

import pytest

from jeff.application.compose_status import ComposeHealth, PublishedPort
from jeff.application.execution import DockerBackendFault
from jeff.infrastructure.docker_cli.compose import (
    ComposeInspectionCLI,
    ComposeStatus,
    _published_ports,
    parse_compose_status,
)

_ROOT = Path('/fixtures/sandbox-project')
_PROJECT = 'sandbox-project'


def _row(*, root: Path = _ROOT, state: str = 'running', oneoff: str = 'False') -> bytes:
    return json.dumps(
        ['a' * 64, _PROJECT, 'api', str(root), str(root / 'compose.yaml'), oneoff, state]
    ).encode()


@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        (b'', ComposeStatus((), False)),
        (_row(), ComposeStatus(('running',), False)),
        (_row(state='exited'), ComposeStatus(('exited',), False)),
        (_row(root=Path('/unrelated')), ComposeStatus((), True)),
        (_row(oneoff='True'), ComposeStatus((), False)),
        (_row(oneoff=''), ComposeStatus((), True)),
    ],
)
def test_project_attribution(data: bytes, expected: ComposeStatus) -> None:
    assert parse_compose_status(data, project=_PROJECT, service='api', root=_ROOT) == expected


@pytest.mark.parametrize('data', [b'{}', _row(state='not-valid'), _row() + b'\n' + _row()])
def test_invalid_metadata_is_not_empty(data: bytes) -> None:
    with pytest.raises(DockerBackendFault, match='docker_protocol_error'):
        parse_compose_status(data, project=_PROJECT, service='api', root=_ROOT)


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("starting", "starting"),
        ("healthy", "healthy"),
        ("unhealthy", "unhealthy"),
        ("not_configured", "not_configured"),
        ("unknown", "unknown"),
        ("unexpected", "unknown"),
        ("state_changed", "unknown"),
        ("labels_changed", "unknown"),
        ("gone", "unknown"),
    ],
)
def test_fixed_read_queries_corroborate_health(
    monkeypatch: pytest.MonkeyPatch, observed: str, expected: ComposeHealth
) -> None:
    end = time.monotonic() + 5
    calls: list[str] = []
    mapping = {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "53129"}]}

    def control(
        self: ComposeInspectionCLI, args: tuple[str, ...], *, endpoint: str, deadline: float
    ) -> bytes:
        assert endpoint == 'unix:///fixture/docker.sock'
        assert deadline == end
        calls.append(args[1])
        if args[1] == "inspect":
            assert args[:3] == ("container", "inspect", "--format")
            assert args[4:] == ("--", "a" * 64)
            assert ".State.Health.Log" not in args[3] and ".Config.Env" not in args[3]
            if observed == "gone":
                raise DockerBackendFault("docker_command_failed")
            row = (
                _row(state="exited") if observed == "state_changed"
                else _row(root=Path("/unrelated")) if observed == "labels_changed"
                else _row()
            )
            assert ".NetworkSettings.Ports" in args[3]
            return json.dumps([*json.loads(row), observed, mapping]).encode()
        assert args[:-1] == (
            'container',
            'ls',
            '--all',
            '--no-trunc',
            '--filter',
            'label=com.docker.compose.project=sandbox-project',
            '--format',
        )
        unrelated = _row(root=Path("/unrelated")).replace(b"a" * 64, b"b" * 64)
        return _row() + b"\n" + unrelated

    monkeypatch.setattr(ComposeInspectionCLI, "_control", control)
    cli = object.__new__(ComposeInspectionCLI)
    status = cli.compose_status(
        endpoint="unix:///fixture/docker.sock",
        project=_PROJECT,
        service="api",
        root=_ROOT,
        deadline=end,
    )
    port = PublishedPort(
        host_ip="127.0.0.1", host_port=53129, container_port=8000, protocol="tcp"
    )
    ports = None if observed in ("state_changed", "labels_changed", "gone") else (port,)
    assert status == ComposeStatus(("running",), True, (expected,), (ports,))
    assert calls == ["ls", "inspect"]


@pytest.mark.parametrize("raw", [None, {}, {"8000/tcp": None}])
def test_no_published_bindings_is_an_empty_list(raw: object) -> None:
    assert _published_ports(raw) == ()


@pytest.mark.parametrize(
    "raw",
    [
        False,
        {"8000/tcp": [{"HostIp": "not-an-ip", "HostPort": "53129"}]},
        {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "65536"}]},
        {"8000/other": [{"HostIp": "127.0.0.1", "HostPort": "53129"}]},
    ],
)
def test_unconfirmed_published_bindings_are_not_empty(raw: object) -> None:
    assert _published_ports(raw) is None


def test_unavailable_is_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> bytes:
        raise DockerBackendFault("docker_unavailable")

    monkeypatch.setattr(ComposeInspectionCLI, "_control", unavailable)
    cli = object.__new__(ComposeInspectionCLI)
    with pytest.raises(DockerBackendFault, match="docker_unavailable"):
        cli.compose_status(
            endpoint="unix:///fixture/docker.sock",
            project=_PROJECT,
            service="api",
            root=_ROOT,
            deadline=42,
        )
