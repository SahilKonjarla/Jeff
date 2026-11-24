"""Small live loopback check with a fake health probe; no Docker or daemon."""

import http.client
import json
import time
from uuid import uuid4

import pytest

from jeff.application.compose_lifecycle import ServiceAttempt, ServiceResources
from jeff.infrastructure.service_health_relay import LoopbackServiceRelays

from test_compose_lifecycle import _attempt, _resources, _start


def test_only_health_is_forwarded_and_shutdown_closes_listener() -> None:
    attempt = _attempt(_start())
    resources = _resources()
    calls: list[tuple[ServiceAttempt, ServiceResources]] = []
    healthy = True

    def probe(actual: ServiceAttempt, observed: ServiceResources, deadline: float) -> bool:
        assert time.monotonic() < deadline
        calls.append((actual, observed))
        return healthy

    relay = LoopbackServiceRelays(attempt.worker_instance_id, probe)
    with pytest.raises(ValueError, match="original reviewed start"):
        relay.open(
            attempt.model_copy(update={"worker_instance_id": uuid4()}),
            resources,
            deadline=time.monotonic() + 2,
        )
    endpoint = relay.open(attempt, resources, deadline=time.monotonic() + 2)

    def request(
        method: str = "GET", path: str = "/health", headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str]]:
        client = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=2)
        try:
            client.request(method, path, headers=headers or {})
            response = client.getresponse()
            return response.status, json.loads(response.read(256))
        finally:
            client.close()

    try:
        assert request() == (200, {"status": "ok"})
        assert request(path="/health?other=1")[0] == 404
        assert request("POST")[0] == 405
        assert request(headers={"Host": "unrelated.invalid"})[0] == 400
        assert request(headers={"Content-Length": "1"})[0] == 400
        assert calls == [(attempt, resources)]
        healthy = False
        assert request() == (503, {"status": "unavailable"})
        assert calls == [(attempt, resources)] * 2
    finally:
        relay.close_all(deadline=time.monotonic() + 3)

    with pytest.raises(ConnectionRefusedError):
        request()
    with pytest.raises(OSError, match="unavailable"):
        relay.open(attempt, resources, deadline=time.monotonic() + 2)
