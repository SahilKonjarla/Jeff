"""Service-only settings compose cold and stay separate from command execution."""

import json
import os
import time
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from jeff.application.compose_lifecycle import ServiceLifecycleSettings
from jeff.application.compose_service_commands import ServiceReviewJournal
from jeff.application.execution import DockerBackendFault, DockerCLIIdentity
from jeff.application.storage import GuardedRuntimeSettings, RuntimeSettings
from jeff.bootstrap.runtime import service_lifecycle as composition
from jeff.infrastructure.docker_cli.service_lifecycle import DockerFixtureService
from jeff.infrastructure.docker_cli.transport import _DockerCLI
from jeff.infrastructure.runtime_settings import parse_settings

from test_compose_lifecycle import _attempt, _resources, _start


@pytest.mark.parametrize("mode", ["off", "on", "binary_mismatch", "source_mismatch"])
def test_private_service_composition_has_no_runtime_side_effects(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = DockerCLIIdentity(path=Path("/fixture/docker"), sha256="a" * 64, size=1)
    source = "print('synthetic')\n"
    start = _start()
    cli = object.__new__(_DockerCLI)
    cli.identity = binary
    adapter = DockerFixtureService(cli, start.target, source.encode())
    start = start.model_copy(update={"configuration_digest": adapter.configuration_digest})
    home_parent = tmp_path.resolve() / "home"
    home_parent.mkdir(mode=0o700)
    homes: list[Path] = []
    settings = ServiceLifecycleSettings(
        start=start, binary=binary, config=Path("/fixture/config"), home=home_parent,
        fixture_source=source if mode != "source_mismatch" else "print('changed')\n",
    )
    raw: dict[str, object] = {"schema_version": 4, "history": {"retention_days": 30}}
    if mode != "off":
        raw["service_lifecycle"] = settings.model_dump(mode="json")
    parsed = parse_settings(json.dumps(raw).encode())
    assert isinstance(parsed, GuardedRuntimeSettings) and parsed.backend is None
    assert parsed.service_lifecycle == (None if mode == "off" else settings)
    assert isinstance(parse_settings(b'{"schema_version":2,"history":{"retention_days":30}}'), RuntimeSettings)

    class PinnedProbe:
        identity = binary.model_copy(update={"sha256": "b" * 64}) if mode == "binary_mismatch" else binary

        def __init__(self, path: Path, config: Path, home: Path, *, deadline: float) -> None:
            assert mode != "off"
            assert (path, config, home.parent) == (binary.path, settings.config, settings.home)
            assert home.stat().st_mode & 0o777 == 0o700 and not list(home.iterdir())
            homes.append(home)
            assert time.monotonic() < deadline

        def __call__(self, *args: object) -> bool:
            raise AssertionError("Startup must not probe a container")

    monkeypatch.setattr(composition, "DockerServiceHealthProbe", PinnedProbe)
    journal = cast(ServiceReviewJournal, object())
    owner = uuid4()
    end = time.monotonic() + 5
    if mode in ("binary_mismatch", "source_mismatch"):
        with pytest.raises(DockerBackendFault, match="docker_unsafe_configuration"):
            composition.compose_service_lifecycle(parsed.service_lifecycle, journal, owner, deadline=end)
        return
    result = composition.compose_service_lifecycle(parsed.service_lifecycle, journal, owner, deadline=end)
    if mode == "off":
        assert result is None
        return
    assert result is not None
    result.relays.seal()
    with pytest.raises(OSError, match="unavailable"):
        start_attempt = _attempt(start).model_copy(update={"worker_instance_id": owner})
        result.relays.open(start_attempt, _resources(), deadline=end)
    result.relays.close_all(deadline=end)
    cache = homes[0] / "Library"
    cache.mkdir()
    previous_umask = os.umask(0o177)
    try:
        replacement = composition.compose_service_lifecycle(
            parsed.service_lifecycle, journal, uuid4(), deadline=end
        )
    finally:
        os.umask(previous_umask)
    assert replacement is not None and replacement.relays is not result.relays
    assert len(homes) == 2 and homes[0] != homes[1] and cache.is_dir()
    replacement.relays.close_all(deadline=end)
