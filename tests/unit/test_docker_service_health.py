"""Exercise service health metadata gates without invoking Docker."""

import json
import time

import pytest

from jeff.infrastructure.docker_cli import service_health

from test_compose_lifecycle import _attempt, _resources, _start


@pytest.mark.parametrize(
    ("fault", "calls"),
    [
        (None, ("info", "container", "network", "exec")),
        ("not_healthy", ("info", "container", "network", "exec")),
        ("engine", ("info",)),
        ("birth", ("info", "container")),
        ("published", ("info", "container")),
        ("extra_network", ("info", "container")),
        ("not_internal", ("info", "container", "network")),
        ("foreign_peer", ("info", "container", "network")),
        ("socket_changed", ("info", "container", "network")),
    ],
)
def test_health_probe_verifies_the_target_before_fixed_exec(
    monkeypatch: pytest.MonkeyPatch, fault: str | None, calls: tuple[str, ...]
) -> None:
    attempt = _attempt(_start())
    resources = _resources()
    probe = object.__new__(service_health.DockerServiceHealthProbe)
    actual: list[str] = []
    deadline = time.monotonic() + 5

    def socket_identity(endpoint: str) -> tuple[int, ...]:
        assert endpoint == attempt.manifest.target.endpoint
        return (2,) if fault == "socket_changed" and actual[-1:] == ["network"] else (1,)

    def control(args: tuple[str, ...], *, endpoint: str, deadline: float) -> bytes:
        assert endpoint == attempt.manifest.target.endpoint and deadline > time.monotonic()
        kind = args[1] if args[:2] == ("container", "exec") else args[0]
        actual.append(kind)
        if kind == "info":
            row: object = "unexpected-engine" if fault == "engine" else _start().target.engine_id
        elif kind == "container":
            assert args[:2] == ("container", "inspect") and args[-2:] == ("--", resources.container.id)
            row = [
                resources.container.id,
                "changed" if fault == "birth" else resources.container.created_at,
                _start().image_id, True, False, {},
                {"8000/tcp": [{"HostPort": "40001"}]} if fault == "published" else {},
                [resources.network.id, "other", None]
                if fault == "extra_network" else [resources.network.id, None],
            ]
        elif kind == "network":
            assert args[:2] == ("network", "inspect") and args[-2:] == ("--", resources.network.id)
            row = [
                resources.network.id, resources.network.created_at, fault != "not_internal", "bridge",
                [resources.container.id, "foreign", None]
                if fault == "foreign_peer" else [resources.container.id, None],
            ]
        else:
            assert args == (
                "container", "exec", "--user", "65534:65534", "--", resources.container.id,
                "/usr/local/bin/python", "-I", "-B", "-c", service_health._PROBE,
            )
            assert 'connection.request("GET","/health")' in args[-1]
            assert 'HTTPConnection("127.0.0.1",8000' in args[-1]
            assert "response.read" not in args[-1]
            return b"unhealthy" if fault == "not_healthy" else b"healthy"
        return json.dumps(row).encode()

    monkeypatch.setattr(probe, "_socket", socket_identity)
    monkeypatch.setattr(probe, "_control", control)
    assert probe(attempt, resources, deadline) is (fault is None)
    assert tuple(actual) == calls
