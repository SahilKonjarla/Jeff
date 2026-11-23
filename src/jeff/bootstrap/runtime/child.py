"""Inherited-role validation and actual adoption before key access; retain raw lock to exit."""

from .. import runtime as _runtime
from .postgres import selected_database, serve_postgres, write_startup


def main() -> int:
    """Validate inherited FDs, inspect metadata, then adopt before reading the key."""
    parser = _runtime.argparse.ArgumentParser(
        description="Internal owned daemon child; use public jeff start."
    )
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--instance-id", type=_runtime.UUID, required=True)
    parser.add_argument("--startup-fd", type=int)
    arguments = parser.parse_args()
    # Fail before location discovery/state access for absent/invalid inherited authority.
    if (
        arguments.lock_fd < 3
        or arguments.startup_fd is None
        or arguments.startup_fd < 3
        or arguments.startup_fd == arguments.lock_fd
    ):
        return 1
    try:
        _runtime.os.fstat(arguments.lock_fd)
        if (
            not _runtime.stat.S_ISFIFO(_runtime.os.fstat(arguments.startup_fd).st_mode)
            or _runtime.fcntl.fcntl(arguments.startup_fd, _runtime.fcntl.F_GETFL)
            & _runtime.os.O_ACCMODE
            != _runtime.os.O_WRONLY
        ):
            return 1
        _runtime.os.umask(0o177)
        store = _runtime.RuntimeStateStore(_runtime._selected_layout())
        owner = store.adopt(arguments.lock_fd)
        identity = _runtime.RuntimeIdentityData(
            instance_id=arguments.instance_id, pid=_runtime.os.getpid()
        )
        token = store.read_credential()
        try:
            database = selected_database(owner)
        except _runtime.StorageFault as error:
            write_startup(arguments.startup_fd, identity, error)
            return 1
        if database is None:
            _runtime.asyncio.run(_runtime._serve(owner, token, identity, arguments.startup_fd))
        else:
            _runtime.asyncio.run(
                serve_postgres(owner, token, identity, arguments.startup_fd, database)
            )
        # Intentionally hold the raw inherited lock FD until kernel process exit, not just
        # until the socket closes. No Python finalizer closes/unlocks the lock description.
        return 0
    except (_runtime.ControlFault, _runtime.StorageFault, OSError, ValueError):
        return 1
