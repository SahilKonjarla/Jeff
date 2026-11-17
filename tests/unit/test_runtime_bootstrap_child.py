"""Child entry metadata/ordering with fake inherited handles; never adopt real state."""

import asyncio
import os
import stat
from typing import cast
from uuid import UUID

import pytest

from jeff.application.runtime import RuntimeIdentityData, RuntimeLayout
from jeff.bootstrap import runtime
from jeff.infrastructure.runtime_state import Ownership


@pytest.mark.parametrize("lock,startup", [(0, None), (3, None), (3, 0), (3, 3)])
def test_invalid_inherited_roles_fail_before_host_or_state_access(
    monkeypatch: pytest.MonkeyPatch, lock: int, startup: int | None
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Invalid inherited authority must fail before state selection")

    argv = ["runtime", "--lock-fd", str(lock), "--instance-id", str(UUID(int=1))]
    if startup is not None:
        argv.extend(("--startup-fd", str(startup)))
    monkeypatch.setattr(runtime.sys, "argv", argv)
    monkeypatch.setattr(runtime, "_selected_layout", forbidden)
    assert runtime.main() == 1


@pytest.mark.parametrize("failure", [None, "pipe_type", "pipe_access", "adoption"])
def test_child_validates_then_adopts_before_key_and_never_closes_raw_lock(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    lock, startup = 100001, 100002
    events: list[str] = []
    owner = cast(Ownership, object())
    layout = cast(RuntimeLayout, object())
    identity = UUID(int=1)
    original_stat, original_close, original_fcntl = os.fstat, os.close, runtime.fcntl.fcntl

    def descriptor(fd: int) -> os.stat_result:
        if fd not in (lock, startup):
            return original_stat(fd)
        events.append("lock" if fd == lock else "pipe")
        mode = stat.S_IFREG if failure == "pipe_type" else stat.S_IFIFO
        return os.stat_result((mode | 0o600, 0, 0, 1, 1, 1, 0, 0, 0, 0))

    def flags(fd: int, command: int) -> int:
        if fd == startup:
            assert command == runtime.fcntl.F_GETFL
            events.append("flags")
            return os.O_RDONLY if failure == "pipe_access" else os.O_WRONLY
        return original_fcntl(fd, command)

    def mask(value: int) -> int:
        assert value == 0o177
        events.append("umask")
        return 0o022

    def selected() -> RuntimeLayout:
        events.append("layout")
        return layout

    class Store:
        def __init__(self, observed: RuntimeLayout) -> None:
            assert observed is layout
            events.append("store")

        def adopt(self, fd: int) -> Ownership:
            assert fd == lock
            events.append("adopt")
            if failure == "adoption":
                raise runtime.ControlFault("runtime_ownership_unconfirmed")
            return owner

        def read_credential(self) -> bytes:
            events.append("credential")
            return bytes(32)  # Fixed synthetic test input only.

    async def serve(
        observed: Ownership, token: bytes, observed_identity: RuntimeIdentityData, result_fd: int
    ) -> None:
        assert observed is owner and token == bytes(32) and result_fd == startup
        assert observed_identity.instance_id == identity
        events.append("serve")
        await asyncio.sleep(0)

    def close(fd: int) -> None:
        if fd in (lock, startup):
            raise AssertionError("main must retain raw inherited descriptors, not finalize them")
        original_close(fd)

    monkeypatch.setattr(
        runtime.sys,
        "argv",
        [
            "runtime",
            "--lock-fd",
            str(lock),
            "--startup-fd",
            str(startup),
            "--instance-id",
            str(identity),
        ],
    )
    monkeypatch.setattr(os, "fstat", descriptor)
    monkeypatch.setattr(runtime.fcntl, "fcntl", flags)
    monkeypatch.setattr(os, "umask", mask)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(runtime, "_selected_layout", selected)
    monkeypatch.setattr(runtime, "RuntimeStateStore", Store)
    monkeypatch.setattr(runtime, "_serve", serve)
    assert runtime.main() == (0 if failure is None else 1)
    if failure == "pipe_type":
        assert events == ["lock", "pipe"]
    elif failure == "pipe_access":
        assert events == ["lock", "pipe", "flags"]
    else:
        assert events[:7] == ["lock", "pipe", "flags", "umask", "layout", "store", "adopt"]
        assert events[7:] == (["credential", "serve"] if failure is None else [])
