"""Explicit project-only PostgreSQL startup, isolated from normal SQLite state."""

import asyncio
import json
import os
import re
import stat
import sys
import time
from contextlib import suppress
from importlib import import_module

from jeff.application.compose_status import ComposeLogsReader, ComposeStatusReader
from jeff.application.execution import DockerBackendFault
from jeff.application.execution.ports import FreshExecutionService
from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    ControlFault,
    PROTOCOL_REVISION,
    RuntimeIdentityData,
    StartupResult,
)
from jeff.application.storage import GuardedRuntimeSettings, StorageFault
from jeff.infrastructure.native_process_ports import read_process_list, read_process_ports
from jeff.infrastructure.postgres_execution_actor import PostgresExecutionActor
from jeff.infrastructure.postgres_project_actor import PostgresProjectActor
from jeff.infrastructure.postgres_storage import PostgresStorage
from jeff.infrastructure.runtime_settings import load_or_create_settings
from jeff.infrastructure.runtime_state import Ownership

from .. import runtime as _runtime
from .compose_status import compose_logs_reader, compose_status_reader
from .postgres_readback import PostgresReadback
from .service_lifecycle import ComposedServiceLifecycle, compose_service_lifecycle


def selected_database(owner: Ownership) -> str | None:
    """Default to PostgreSQL; only explicit legacy fixtures may omit the backend."""
    owner.check()
    directory = owner.store.layout.data_dir
    marker = directory / "postgresql.json"
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if os.environ.get("JEFF_TEST_SQLITE_FIXTURE") == "1":
            return None
        _reject_sqlite_state(owner)
        return "jeff"
    except OSError:
        raise StorageFault("settings_invalid") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 1 <= info.st_size <= 256
        ):
            raise StorageFault("settings_invalid")
        raw = os.read(fd, 257)
        if len(raw) != info.st_size:
            raise StorageFault("settings_invalid")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"database"}
            or not isinstance(name := value["database"], str)
            or (name != "jeff" and not re.fullmatch(r"jeff_cli_proof_[a-z0-9]{6,32}", name))
        ):
            raise StorageFault("settings_invalid")
        _reject_sqlite_state(owner)
        return name
    except (OSError, ValueError):
        raise StorageFault("settings_invalid") from None
    finally:
        os.close(fd)


def _reject_sqlite_state(owner: Ownership) -> None:
    # A missing marker is not permission to overwrite or ignore old state.
    for suffix in ("", "-journal", "-wal", "-shm"):
        if os.path.lexists(str(owner.store.layout.database) + suffix):
            raise StorageFault("storage_unsafe")
    owner.check()


def write_startup(
    result_fd: int, identity: RuntimeIdentityData, error: StorageFault | None
) -> None:
    try:
        with suppress(BrokenPipeError):
            os.write(
                result_fd,
                _runtime.encode_message(
                    StartupResult(
                        revision=PROTOCOL_REVISION,
                        instance_id=identity.instance_id,
                        error=None if error is None else error.code,
                    ).model_dump(mode="json"),
                    512,
                ),
            )
    finally:
        os.close(result_fd)


async def serve_postgres(
    owner: Ownership, token: bytes, identity: RuntimeIdentityData, result_fd: int, database: str
) -> None:
    """Use native authenticated IPC and Git, with no SQLite fallback."""

    compose_reader: ComposeStatusReader | None = None
    logs_reader: ComposeLogsReader | None = None
    services: ComposedServiceLifecycle | None = None

    def actor_for_password() -> PostgresProjectActor:
        nonlocal compose_reader, logs_reader, services
        try:
            raw = sys.stdin.buffer.read(513)
            sys.stdin.close()
            if not 1 <= len(raw) <= 512 or b"\0" in raw:
                raise StorageFault("settings_invalid")
            password = raw.decode("utf-8")
            driver = import_module("psycopg")
        except (ImportError, UnicodeError, OSError):
            raise StorageFault("storage_failed") from None
        adapter = PostgresStorage(
            lambda: driver.connect(
                host="127.0.0.1",
                port=55432,
                dbname=database,
                user="jeff_app" if database == "jeff" else database,
                password=password,
                connect_timeout=2,
                sslmode="disable",
            ),
            driver.Error,
            identity.instance_id,
        )
        settings, settings_source = load_or_create_settings(owner)
        if isinstance(settings, GuardedRuntimeSettings) and settings.compose_status is not None:
            compose_reader = compose_status_reader(settings.compose_status)
            logs_reader = compose_logs_reader(settings.compose_status)
        if isinstance(settings, GuardedRuntimeSettings) and (
            settings.backend is not None or settings.service_lifecycle is not None
        ):
            execution_actor = PostgresExecutionActor(
                owner,
                adapter,
                settings,
                on_commit=lambda: events.signal(),
                settings_source=settings_source,
            )
            try:
                services = compose_service_lifecycle(
                    settings.service_lifecycle,
                    execution_actor,
                    identity.instance_id,
                    deadline=time.monotonic() + DEFAULT_RUNTIME_LIMITS.start_seconds,
                )
            except DockerBackendFault:
                execution_actor.close()
                raise StorageFault("settings_invalid") from None
            return execution_actor
        return PostgresProjectActor(
            adapter,
            on_commit=lambda: events.signal(),
            retention_days=settings.history.retention_days,
            settings_source=settings_source,
        )

    actor = None
    try:
        actor = actor_for_password()
        actor.start()
    except (StorageFault, TimeoutError) as error:
        fault = error if isinstance(error, StorageFault) else StorageFault("storage_busy")
        write_startup(result_fd, identity, fault)
        if actor is not None:
            actor.close()
        raise fault from None
    events = _runtime.RuntimeEvents(
        identity,
        _runtime._stream_send,
        lambda: actor.snapshot().state == "ready",
        actor.retention_position,
    )
    runner = _runtime.OwnedGitRunner(lambda: _duplicate_owner(owner))
    projects = _runtime.ProjectService(
        _runtime.GitProjectDiscovery(process_runner=runner), _runtime.FileProjectStore()
    )
    fresh: FreshExecutionService = PostgresReadback(projects, actor, identity.instance_id)
    inspection_only = True
    if isinstance(actor, PostgresExecutionActor) and actor.execution_backend is not None:
        configured = _runtime.create_configured_execution(
            owner=owner,
            storage=actor,
            projects=projects,
            instance_id=identity.instance_id,
            deadline=time.monotonic() + DEFAULT_RUNTIME_LIMITS.start_seconds,
        )
        if isinstance(configured, _runtime.ExecutionCoordinator):
            fresh, inspection_only = configured, False
    dispatcher = _runtime.RuntimeDispatcher(
        _runtime.ProjectDispatcher(
            projects,
            _runtime.QuestionService(projects, _runtime.GitRepositoryRead(process_runner=runner)),
            actor,
            identity,
            compose_status=compose_reader,
            compose_logs=logs_reader,
            process_ports=read_process_ports,
            process_list=read_process_list,
            service_commands=services.commands if services is not None else None,
        ),
        fresh=fresh,
        inspection_only=inspection_only,
        service_worker=services.worker if services is not None else None,
    )
    maintenance_stop = asyncio.Event()

    def on_stop() -> None:
        maintenance_stop.set()
        if services is not None:
            services.relays.seal()

    server = _runtime.ControlServer(owner, token, identity, actor.snapshot, on_stop=on_stop)
    server.events = events
    maintenance: asyncio.Task[None] | None = None
    try:
        dispatcher.start()
        server.dispatcher = dispatcher
        events.start(dispatcher)
        maintenance = asyncio.create_task(_maintain(actor, maintenance_stop))
        write_startup(result_fd, identity, None)
        await server.serve()
    finally:
        server.request_stop()
        try:
            if maintenance is not None:
                await maintenance
            dispatcher.request_close()
            while not dispatcher.closed or not runner.closed:
                server.check_stop_budget()
                await asyncio.sleep(0.01)
            dispatcher.close(timeout=0)
        finally:
            try:
                if services is not None:
                    await asyncio.to_thread(services.relays.close_all, deadline=server.stop_deadline)
            except TimeoutError:
                raise ControlFault("runtime_stop_unconfirmed") from None
        actor.close()


async def _maintain(actor: PostgresProjectActor, stopping: asyncio.Event) -> None:
    while not stopping.is_set():
        try:
            await asyncio.to_thread(actor.maintain_retention)
        except StorageFault as error:
            if error.code != "storage_busy":
                return  # An uncertain transaction is never resubmitted by the scheduler.
        with suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=30)


def _duplicate_owner(owner: Ownership) -> int:
    owner.check()
    return os.dup(owner.fd)
