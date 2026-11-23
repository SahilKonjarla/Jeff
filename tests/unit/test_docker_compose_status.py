"""Read-only Compose projection and fail-closed fixture matching."""

import json
from pathlib import Path

import pytest

from jeff.application.execution import DockerBackendFault
from jeff.infrastructure.docker_cli.compose import (
    ComposeInspectionCLI,
    ComposeStatus,
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


def test_only_fixed_read_query_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    def control(
        self: ComposeInspectionCLI, args: tuple[str, ...], *, endpoint: str, deadline: float
    ) -> bytes:
        assert args[:-1] == (
            'container',
            'ls',
            '--all',
            '--no-trunc',
            '--filter',
            'label=com.docker.compose.project=sandbox-project',
            '--format',
        )
        assert endpoint == 'unix:///fixture/docker.sock'
        assert deadline == 42
        return _row()

    monkeypatch.setattr(ComposeInspectionCLI, "_control", control)
    cli = object.__new__(ComposeInspectionCLI)
    status = cli.compose_status(
        endpoint="unix:///fixture/docker.sock",
        project=_PROJECT,
        service="api",
        root=_ROOT,
        deadline=42,
    )
    assert status == ComposeStatus(("running",), False)


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
