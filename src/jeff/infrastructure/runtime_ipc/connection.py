"""Native-peer authenticated unary connections and explicit control clients."""

import os
import socket
import stat
import time
from typing import Self
from uuid import UUID, uuid4

from jeff.application.runtime import (
    DEFAULT_RUNTIME_LIMITS,
    PROTOCOL_REVISION,
    AuthAccepted,
    ControlFault,
    ControlObservation,
    ControlOperation,
    ControlRequest,
    ControlResponse,
    RuntimeLayout,
)
from jeff.infrastructure.runtime_state import RuntimeStateStore

from .. import runtime_ipc as ipc


class ControlConnection:
    """One authenticated unary connection. No replay, no credential-bearing repr/result."""

    def __init__(self, layout: RuntimeLayout, token: bytes, deadline: float) -> None:
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.deadline = deadline
        try:
            store = RuntimeStateStore(layout)
            with store.directory(layout.runtime_dir, create=False) as root:
                try:
                    before = os.stat("daemon.sock", dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    raise ControlFault("runtime_unreachable") from None
                if not (
                    stat.S_ISSOCK(before.st_mode)
                    and before.st_uid == layout.uid
                    and stat.S_IMODE(before.st_mode) == 0o600
                    and before.st_nlink == 1
                ):
                    raise ControlFault("unsafe_runtime_path")
                self.connection.settimeout(ipc._remaining(deadline))
                try:
                    self.connection.connect(str(layout.socket))
                except OSError:
                    raise ControlFault("runtime_unreachable") from None
                uid, self.peer_pid = ipc.peer_identity(self.connection)
                if uid != layout.uid:
                    raise ControlFault("runtime_peer_unverified")
                after = os.stat("daemon.sock", dir_fd=root, follow_symlinks=False)
                if (before.st_dev, before.st_ino, before.st_mode, before.st_uid) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_uid,
                ):
                    raise ControlFault("runtime_ownership_unconfirmed")
        except (OSError, ControlFault) as error:
            self.connection.close()
            if isinstance(error, ControlFault):
                raise
            raise ControlFault("runtime_unreachable") from None
        try:
            auth_deadline = min(deadline, time.monotonic() + DEFAULT_RUNTIME_LIMITS.auth_seconds)
            self.connection.settimeout(ipc._remaining(auth_deadline))
            self.connection.sendall(
                ipc.encode_message(
                    {"revision": PROTOCOL_REVISION, "token": token.hex()},
                    DEFAULT_RUNTIME_LIMITS.auth_bytes,
                )
            )
            reply = ipc.receive_message(
                self.connection, DEFAULT_RUNTIME_LIMITS.auth_bytes, auth_deadline, AuthAccepted
            )
            if reply.error is not None:
                raise ControlFault(reply.error)
            if reply.identity is None or reply.identity.pid != self.peer_pid:
                raise ControlFault("runtime_peer_unverified")
            self.identity = reply.identity
        except (OSError, ControlFault) as error:
            self.connection.close()
            if isinstance(error, ControlFault) and error.code in (
                "runtime_protocol_incompatible",
                "runtime_authentication_failed",
                "runtime_peer_unverified",
            ):
                raise
            raise ControlFault("runtime_handshake_unconfirmed") from None
        self.used = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.close()

    def request(
        self, operation: ControlOperation, expected_instance_id: UUID | None = None
    ) -> ControlObservation:
        if self.used:
            raise ControlFault("runtime_protocol_error")
        self.used = True
        request = ControlRequest(
            revision=PROTOCOL_REVISION,
            request_id=uuid4(),
            operation=operation,
            expected_instance_id=expected_instance_id,
        )
        try:
            deadline = min(self.deadline, time.monotonic() + DEFAULT_RUNTIME_LIMITS.frame_seconds)
            self.connection.settimeout(ipc._remaining(deadline))
            self.connection.sendall(
                ipc.encode_message(
                    request.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes
                )
            )
            response = ipc.receive_message(
                self.connection, DEFAULT_RUNTIME_LIMITS.response_bytes, deadline, ControlResponse
            )
            if response.request_id != request.request_id:
                raise ControlFault("runtime_protocol_error")
            if response.error is not None:
                raise ControlFault(response.error)
            observation = response.observation
            if (
                observation is None
                or observation.identity is None
                or observation.identity.pid != self.peer_pid
                or observation.identity != self.identity
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            if (
                expected_instance_id is not None
                and observation.identity.instance_id != expected_instance_id
            ):
                raise ControlFault("runtime_ownership_unconfirmed")
            return observation
        except OSError:
            raise ControlFault("runtime_unreachable") from None


class ControlClient:
    """Open bounded native-peer/authenticated connections without owning daemon lifetime."""

    def __init__(self, layout: RuntimeLayout, token: bytes) -> None:
        if len(token) != 32:
            raise ControlFault("runtime_credentials_invalid")
        self.layout = layout
        self._token = token

    def connect(self, deadline: float) -> ControlConnection:
        return ControlConnection(self.layout, self._token, deadline)

    def request(
        self,
        operation: ControlOperation,
        *,
        deadline: float,
        expected_instance_id: UUID | None = None,
    ) -> ControlObservation:
        with self.connect(deadline) as connection:
            return connection.request(operation, expected_instance_id)


def inspect_control(layout: RuntimeLayout, deadline: float) -> ControlObservation:
    """Diagnostic status via existing safe credential/native client; no lifecycle/SQL I/O."""
    ipc._remaining(deadline)
    store = RuntimeStateStore(layout)
    client = ControlClient(layout, store.read_credential())
    ipc._remaining(deadline)
    return client.request("status", deadline=deadline)
