"""One serving/activation/drain lifetime; original actor, helper and SQL ordering."""

from jeff.application.runtime import RuntimeIdentityData
from jeff.infrastructure.runtime_state import Ownership

from .. import runtime as _runtime


async def _serve(
    owner: Ownership, token: bytes, identity: RuntimeIdentityData, result_fd: int
) -> None:
    """Compose one owner and drain actual worker/helper/SQL lifetimes before exit.

    The startup pipe is diagnostic only; authenticated full readiness remains the
    parent's authority. Shutdown waits for resources rather than cancelling waits.
    """
    activation_deadline = _runtime.time.monotonic() + _runtime.DEFAULT_RUNTIME_LIMITS.start_seconds
    events = _runtime.RuntimeEvents(
        identity,
        _runtime._stream_send,
        lambda: storage.snapshot().state == "ready" and storage.notification_healthy,
        lambda: storage.retention_position(),
    )
    # D3 invalidates buffered pages using checked committed positions before loop hints.
    storage = _runtime.SQLiteStorage(
        owner,
        instance_id=identity.instance_id,
        commit_hint=events.signal,
        scheduled_retention=True,
    )
    dispatcher: _runtime.RuntimeDispatcher | None = None

    def duplicate_git_lease() -> int:
        """Share the existing flock description; a newly opened lock would not do so."""
        owner.check()
        return _runtime.os.dup(owner.fd)

    git_runner = _runtime.OwnedGitRunner(duplicate_git_lease)
    server = _runtime.ControlServer(
        owner, token, identity, storage.snapshot, on_stop=storage.pause_retention
    )
    server.events = events
    serving = _runtime.asyncio.create_task(server.serve())
    try:
        storage.start()
        while storage.snapshot().state == "initializing":
            if serving.done():
                await serving
            await _runtime.asyncio.sleep(0.01)
        if serving.done():
            await serving  # publication/listener failure cannot become application readiness
        state = storage.snapshot()
        if state.state == "ready" and not server.stopping:
            projects = _runtime.ProjectService(
                _runtime.GitProjectDiscovery(process_runner=git_runner), _runtime.FileProjectStore()
            )
            dispatcher = _runtime.RuntimeDispatcher(
                _runtime.ProjectDispatcher(
                    projects,
                    _runtime.QuestionService(
                        projects, _runtime.GitRepositoryRead(process_runner=git_runner)
                    ),
                    storage,
                    identity,
                ),
                _runtime.ExecutionService(
                    projects, storage, lambda: storage.execution_profiles, identity.instance_id
                ),
                fresh_factory=(
                    lambda: _runtime.create_configured_execution(
                        owner=owner,
                        storage=storage,
                        projects=projects,
                        instance_id=identity.instance_id,
                        deadline=activation_deadline,
                    )
                )
                if storage.execution_backend is not None
                else None,
            )
            dispatcher.start()
            server.dispatcher = dispatcher
            try:
                await _runtime.asyncio.shield(
                    _runtime.asyncio.wrap_future(dispatcher.initialization)
                )
            except (_runtime.ControlFault, _runtime.StorageFault):
                raise
            except Exception:
                raise _runtime.ControlFault("runtime_start_failed") from None
            if not server.stopping:
                events.start(dispatcher)
        packet = _runtime.encode_message(
            _runtime.StartupResult(
                revision=_runtime.PROTOCOL_REVISION,
                instance_id=identity.instance_id,
                error=state.error,
            ).model_dump(mode="json"),
            512,
        )
        try:
            if _runtime.os.write(result_fd, packet) != len(packet):
                raise _runtime.ControlFault("runtime_start_failed")
        except BrokenPipeError:
            pass  # authenticated readiness can precede this diagnostic-only result
        finally:
            _runtime.os.close(result_fd)
            result_fd = -1
        if state.state != "ready":
            server.request_stop()
        await serving
    finally:
        if result_fd >= 0:
            _runtime.os.close(result_fd)
        server.request_stop()
        try:
            await serving
        finally:
            # No successful shutdown after the original absolute budget. If a
            # cooperative syscall/thread outlives it, main still retains the raw
            # inherited ownership FD until kernel exit; no PID signalling/adoption.
            try:
                if dispatcher is not None:
                    dispatcher.request_close()
                    while not dispatcher.closed:
                        server.check_stop_budget()
                        await _runtime.asyncio.sleep(0.01)
                    dispatcher.close(timeout=0)
                while not git_runner.closed:
                    server.check_stop_budget()
                    await _runtime.asyncio.sleep(0.01)
            finally:
                # Seal SQL even on drain failure, without claiming a live actor has exited.
                storage.request_close()
                while not storage.closed:
                    server.check_stop_budget()
                    await _runtime.asyncio.sleep(0.01)
                storage.close(timeout=0)
