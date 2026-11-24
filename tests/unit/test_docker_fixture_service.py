"""Exercise the fixture adapter using only a synthetic Docker transport."""

import json
import time
from pathlib import Path

import pytest

from jeff.application.compose_lifecycle import ServiceRestart
from jeff.application.execution import DockerBackendFault, DockerCLIIdentity
from jeff.infrastructure.docker_cli.service_lifecycle import DockerFixtureService
from jeff.infrastructure.docker_cli.transport import _DockerCLI

from test_compose_lifecycle import _attempt, _resources, _start, _stop


@pytest.mark.parametrize(
    ("fault", "mutations"),
    [
        (None, ["network.create", "container.create", "container.start", "container.restart", "container.stop"]),
        ("restart_drift", ["network.create", "container.create", "container.start"]),
        ("digest", []),
        ("collision", []),
        ("engine", []),
        ("image", []),
        ("image_volume", []),
        ("ambiguous_create", ["network.create", "container.create"]),
        ("created_policy", ["network.create", "container.create"]),
        ("start_drift", ["network.create", "container.create"]),
        ("stop_drift", ["network.create", "container.create", "container.start"]),
    ],
)
def test_fixture_actions_verify_exact_target_and_never_retry(
    monkeypatch: pytest.MonkeyPatch, fault: str | None, mutations: list[str]
) -> None:
    source = b"print('synthetic fixture')\n"
    start = _start()
    resources = _resources()
    container, network = resources.container, resources.network
    cli = object.__new__(_DockerCLI)
    cli.identity = DockerCLIIdentity(path=Path("/fixture/docker"), sha256="b" * 64, size=1)
    adapter = DockerFixtureService(cli, start.target, source)
    if fault != "digest":
        start = start.model_copy(update={"configuration_digest": adapter.configuration_digest})
    attempt = _attempt(start)
    expected_argv = ["-I", "-B", "-c", source.decode(), "--host", "0.0.0.0", "--port", "8000"]
    seen: list[str] = []
    state = "absent"
    drift = False
    deadline = time.monotonic() + 5

    def control(args: tuple[str, ...], *, endpoint: str, deadline: float) -> bytes:
        nonlocal state
        assert endpoint == start.target.endpoint and deadline > time.monotonic()
        if args[0] == "info":
            value: object = "other" if fault == "engine" else start.target.engine_id
        elif args[:2] == ("image", "inspect"):
            assert args[-2:] == ("--", start.image_id)
            assert '(index .Config "Volumes")' in args[3]
            value = [
                start.image_id,
                "linux",
                "amd64" if fault == "image" else "arm64",
                {"/unexpected": {}} if fault == "image_volume" else None,
            ]
        elif args[1] == "ls":
            return b"collision\n" if fault == "collision" else b""
        elif args[:2] == ("network", "create"):
            seen.append("network.create")
            assert "--internal" in args and "bridge" in args
            return (network.id + "\n").encode()
        elif args[:2] == ("container", "create"):
            seen.append("container.create")
            assert "--pull=never" in args and "--no-healthcheck" in args
            assert args[args.index("--network") + 1] == network.id
            assert list(args[-8:]) == expected_argv
            assert "--publish" not in args and "--mount" not in args
            if fault == "ambiguous_create":
                raise DockerBackendFault("docker_mutation_unconfirmed")
            state = "created"
            return (container.id + "\n").encode()
        elif args[:2] == ("container", "start"):
            assert args == ("container", "start", "--", container.id) and state == "created"
            seen.append("container.start")
            state = "running"
            return b""
        elif args[:2] == ("container", "restart"):
            assert args == ("container", "restart", "--time", "3", "--", container.id)
            assert state == "running"
            seen.append("container.restart")
            return b""
        elif args[:2] == ("container", "stop"):
            assert args == ("container", "stop", "--time", "3", "--", container.id)
            seen.append("container.stop")
            state = "exited"
            return b""
        elif args[:2] == ("network", "inspect"):
            assert args[-2:] == ("--", network.id)
            value = [
                network.id, network.created_at, True, "bridge", str(attempt.job_id), start.digest,
                [container.id, None] if state == "running" else [None],
            ]
        else:
            assert args[:2] == ("container", "inspect") and args[-2:] == ("--", container.id)
            value = [
                container.id, network.created_at if drift else container.created_at,
                start.image_id, state, str(attempt.job_id), start.digest,
                False, {}, {}, network.id, [network.id, None],
                0 if fault == "created_policy" else start.memory_bytes,
                start.nano_cpus, start.pids_limit, "65534:65534", False, True,
                ["ALL"], ["no-new-privileges=true"], [], start.memory_bytes, "no",
                "/usr/local/bin/python", expected_argv,
            ]
        return json.dumps(value).encode()

    monkeypatch.setattr(cli, "_control", control)
    monkeypatch.setattr(cli, "_socket", lambda endpoint: (1,))

    def lifecycle() -> None:
        assert adapter.verify(start, deadline=deadline) == start.digest
        assert adapter.create(attempt, deadline=deadline) == resources
        nonlocal drift
        drift = fault == "start_drift"
        assert adapter.start(attempt, resources, deadline=deadline) == resources
        stop = _stop(attempt, resources)
        if fault in (None, "restart_drift"):
            restart = ServiceRestart.model_validate(
                stop.model_dump() | {"action": "restart", "original": start}
            )
            assert adapter.verify(restart, deadline=deadline) == restart.digest
            drift = fault == "restart_drift"
            assert adapter.restart(_attempt(restart), deadline=deadline) == resources
        assert adapter.verify(stop, deadline=deadline) == stop.digest
        drift = fault == "stop_drift"
        assert adapter.stop(_attempt(stop), deadline=deadline) == resources

    if fault is None:
        lifecycle()
    else:
        with pytest.raises(DockerBackendFault):
            lifecycle()
    assert seen == mutations
