"""Internal control child composition; not a public CLI lifecycle command."""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import cast
from uuid import UUID

from jeff.application.projects import Failure
from jeff.application.runtime import (
    ControlFault,
    ControlIssueCode,
    RuntimeControl,
    RuntimeIdentityData,
    RuntimeLayout,
    RuntimePathInputs,
)
from jeff.infrastructure.runtime_ipc import ControlServer
from jeff.infrastructure.runtime_paths import resolve_layout
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore


def _selected_layout() -> RuntimeLayout:
    if sys.platform != "darwin":
        raise ControlFault("unsupported_runtime_platform")
    data = os.environ.get("JEFF_DATA_DIR")
    runtime = os.environ.get("JEFF_RUNTIME_DIR")
    # tempfile.gettempdir() probes writability by creating files. Selection/status must
    # not do that. Use standard Darwin TMPDIR metadata (or the canonical OS /tmp base),
    # and do not even resolve unused host locations when real overrides are supplied.
    layout = resolve_layout(
        RuntimePathInputs(
            platform=sys.platform,
            home=Path.home() if data is None else Path("/"),
            temp_dir=Path(os.environ.get("TMPDIR", "/private/tmp"))
            if runtime is None
            else Path("/"),
            uid=os.geteuid(),
            data_dir=data,
            runtime_dir=runtime,
        )
    )
    if isinstance(layout, Failure):
        raise ControlFault(cast(ControlIssueCode, layout.error.code))
    return layout


def create_runtime_controller() -> RuntimeControl:
    """Select only explicit overrides/needed host metadata; no filesystem probes."""
    return RuntimeController(_selected_layout())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Internal owned control child; no application readiness."
    )
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--instance-id", type=UUID, required=True)
    arguments = parser.parse_args()
    # Fail before location discovery/state access for absent/invalid inherited authority.
    if arguments.lock_fd < 3:
        return 1
    try:
        os.fstat(arguments.lock_fd)
        os.umask(0o177)
        store = RuntimeStateStore(_selected_layout())
        owner = store.adopt(arguments.lock_fd)
        asyncio.run(
            ControlServer(
                owner,
                store.read_credential(),
                RuntimeIdentityData(
                    instance_id=arguments.instance_id,
                    pid=os.getpid(),
                ),
            ).serve()
        )
        # Intentionally hold the raw inherited lock FD until kernel process exit, not just
        # until the socket closes. No Python finalizer closes/unlocks the lock description.
        return 0
    except (ControlFault, OSError, ValueError):
        return 1  # no raw exceptions/credentials/frames on stderr


if __name__ == "__main__":
    raise SystemExit(main())
