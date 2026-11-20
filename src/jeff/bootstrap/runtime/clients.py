"""Lazy location and public client factories; no installation selection at import time."""

from jeff.application.diagnostics import RuntimeDiagnostics
from jeff.application.dispatch import ActivityClient, ProjectClient
from jeff.application.execution import ExecutionCancelClient, ExecutionClient, FreshExecutionClient
from jeff.application.runtime import RuntimeControl, RuntimeLayout

from .. import runtime as _runtime


def _selected_layout() -> RuntimeLayout:
    """Select needed host inputs lazily, without probing or preparing installation state."""
    if _runtime.sys.platform != "darwin":
        raise _runtime.ControlFault("unsupported_runtime_platform")
    data = _runtime.os.environ.get("JEFF_DATA_DIR")
    runtime = _runtime.os.environ.get("JEFF_RUNTIME_DIR")
    # tempfile.gettempdir() probes writability by creating files. Selection/status must
    # not do that. Use standard Darwin TMPDIR metadata (or the canonical OS /tmp base),
    # and do not even resolve unused host locations when real overrides are supplied.
    layout = _runtime.resolve_layout(
        _runtime.RuntimePathInputs(
            platform=_runtime.sys.platform,
            home=_runtime.Path.home() if data is None else _runtime.Path("/"),
            temp_dir=_runtime.Path(_runtime.os.environ.get("TMPDIR", "/private/tmp"))
            if runtime is None
            else _runtime.Path("/"),
            uid=_runtime.os.geteuid(),
            data_dir=data,
            runtime_dir=runtime,
        )
    )
    if isinstance(layout, _runtime.Failure):
        raise _runtime.ControlFault(_runtime.cast(_runtime.ControlIssueCode, layout.error.code))
    return layout


def create_runtime_diagnostics() -> RuntimeDiagnostics:
    """Lazy doctor composition; selection/inspection creates no installation state."""
    from jeff.infrastructure.runtime_diagnostics import RuntimeDiagnosticsAdapter

    return RuntimeDiagnosticsAdapter(_runtime._selected_layout)


def create_runtime_controller() -> RuntimeControl:
    """Select only explicit overrides/needed host metadata; no filesystem probes."""
    return _runtime.RuntimeController(_runtime._selected_layout())


def create_project_client() -> ProjectClient:
    """Lazy location selection; local Git is composed only for read-only status."""
    return _runtime.RuntimeProjectClient(
        _runtime._selected_layout,
        diagnostics=_runtime.ProjectStatusService(
            _runtime.ProjectService(_runtime.GitProjectDiscovery(), _runtime.FileProjectStore()),
            _runtime.create_runtime_controller,
        ),
    )


def create_execution_client() -> ExecutionClient:
    """Review-only RPC client; no installation or backend access at construction."""
    return _runtime.RuntimeProjectClient(_runtime._selected_layout)


def create_cancellation_client() -> ExecutionCancelClient:
    """Lazy authenticated cancellation client; no cwd, defaults discovery or job adoption."""
    return _runtime.RuntimeProjectClient(_runtime._selected_layout)


def create_fresh_execution_client() -> FreshExecutionClient:
    """Fresh RPC client only; constructing it never enables a daemon backend."""
    return _runtime.RuntimeProjectClient(_runtime._selected_layout)


def create_activity_client() -> ActivityClient:
    """Lazy installed subscription interface; no CLI/auto-start/cursor persistence."""
    return _runtime.RuntimeProjectClient(_runtime._selected_layout)
