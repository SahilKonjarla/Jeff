"""Cold opt-in composition for a single native service worker and its relays."""

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from jeff.application.compose_lifecycle import ServiceLifecycleSettings
from jeff.application.compose_service_commands import ServiceCommands, ServiceReviewJournal
from jeff.application.compose_service_worker import ServiceWorker
from jeff.application.execution import DockerBackendFault
from jeff.infrastructure.docker_cli.service_health import DockerServiceHealthProbe
from jeff.infrastructure.docker_cli.service_lifecycle import DockerFixtureService
from jeff.infrastructure.docker_cli.transport import _safe_ancestors
from jeff.infrastructure.service_health_relay import LoopbackServiceRelays


@dataclass(frozen=True)
class ComposedServiceLifecycle:
    worker: ServiceWorker
    relays: LoopbackServiceRelays
    commands: ServiceCommands


def _fresh_home(parent: Path, config: Path, *, deadline: float) -> Path:
    if (
        parent.resolve(strict=True) != parent
        or parent == config
        or parent.is_relative_to(config)
        or config.is_relative_to(parent)
    ):
        raise DockerBackendFault("docker_unsafe_configuration")
    _safe_ancestors(parent, deadline=deadline)
    info = parent.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DockerBackendFault("docker_unsafe_configuration")
    # Keep old caches untouched; a bounded parent cannot grow indefinitely.
    with os.scandir(parent) as entries:
        if sum(1 for _, _entry in zip(range(32), entries, strict=False)) >= 32:
            raise DockerBackendFault("docker_home_capacity")
    home = Path(tempfile.mkdtemp(prefix="service-", dir=parent))
    # The daemon's file-oriented umask strips owner search permission from mkdir.
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return home


def compose_service_lifecycle(
    settings: ServiceLifecycleSettings | None,
    journal: ServiceReviewJournal,
    owner: UUID,
    *,
    deadline: float,
) -> ComposedServiceLifecycle | None:
    """Validate local inputs only. No Docker query, listener, or database migration."""
    if settings is None:
        return None
    try:
        home = _fresh_home(settings.home, settings.config, deadline=deadline)
        probe = DockerServiceHealthProbe(
            settings.binary.path, settings.config, home, deadline=deadline
        )
        if probe.identity != settings.binary:
            raise DockerBackendFault("docker_unsafe_configuration")
        backend = DockerFixtureService(
            probe,
            settings.start.target,
            settings.fixture_source.encode("utf-8"),
            configured_start=settings.start,
        )
    except (OSError, UnicodeError):
        raise DockerBackendFault("docker_unsafe_configuration") from None
    relays = LoopbackServiceRelays(owner, probe)
    worker = ServiceWorker(journal, backend, owner, relays=relays)
    commands = ServiceCommands(journal, backend, worker, settings.start, owner)
    return ComposedServiceLifecycle(worker, relays, commands)
