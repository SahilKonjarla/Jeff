"""Bounded daemon-owned loopback HTTP health listener; no Docker integration."""

import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from jeff.application.compose_lifecycle import ServiceAttempt, ServiceResources
from jeff.application.compose_service_worker import HealthRelayEndpoint

_HEADER_LIMIT = 2048
_REQUEST_SECONDS = 2.0
_ANSWER = {
    200: (b"OK", b'{"status":"ok"}\n'),
    400: (b"Bad Request", b'{"status":"invalid_request"}\n'),
    404: (b"Not Found", b'{"status":"not_found"}\n'),
    405: (b"Method Not Allowed", b'{"status":"method_not_allowed"}\n'),
    503: (b"Service Unavailable", b'{"status":"unavailable"}\n'),
}


@dataclass
class _Listener:
    socket: socket.socket
    closing: threading.Event
    thread: threading.Thread


def _request_code(request: bytes, port: int) -> int:
    head, delimiter, remainder = request.partition(b"\r\n\r\n")
    if not delimiter or remainder or any(byte not in b"\r\n\t" and not 32 <= byte <= 126 for byte in head):
        return 400
    line, *fields = head.split(b"\r\n")
    parts = line.split(b" ")
    if len(parts) != 3 or parts[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
        return 400
    headers: dict[bytes, bytes] = {}
    for field in fields:
        key, colon, value = field.partition(b":")
        if not colon or not re.fullmatch(rb"[A-Za-z0-9-]+", key) or key.lower() in headers:
            return 400
        headers[key.lower()] = value.strip(b" \t")
    if (
        headers.get(b"host") != f"127.0.0.1:{port}".encode("ascii")
        or b"transfer-encoding" in headers
        or headers.get(b"content-length", b"0") != b"0"
        or b"upgrade" in headers
    ):
        return 400
    if parts[0] != b"GET":
        return 405
    return 200 if parts[1] == b"/health" else 404


def _serve_request(
    client: socket.socket, port: int, closing: threading.Event, probe: Callable[[float], bool]
) -> None:
    deadline = time.monotonic() + _REQUEST_SECONDS
    read_deadline = min(deadline, time.monotonic() + 0.5)
    request = bytearray()
    while b"\r\n\r\n" not in request and len(request) < _HEADER_LIMIT:
        remaining = read_deadline - time.monotonic()
        if remaining <= 0 or closing.is_set():
            return
        client.settimeout(remaining)
        block = client.recv(_HEADER_LIMIT - len(request))
        if not block:
            return
        request.extend(block)
    code = _request_code(bytes(request), port)
    if code == 200:
        if closing.is_set():
            return
        try:
            passed = probe(deadline) is True
        except Exception:
            passed = False
        code = 200 if passed and time.monotonic() < deadline else 503
    if closing.is_set():
        return
    status, body = _ANSWER[code]
    response = (
        f"HTTP/1.1 {code} ".encode("ascii") + status
        + b"\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nConnection: close"
        + f"\r\nContent-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    )
    client.settimeout(0.25)
    client.sendall(response)


class LoopbackServiceRelays:
    """One sequential listener per registered start, with a bounded registry."""

    def __init__(
        self, owner: UUID, probe: Callable[[ServiceAttempt, ServiceResources, float], bool]
    ) -> None:
        self._owner = owner
        self._probe = probe
        self._lock = threading.Lock()
        self._listeners: dict[UUID, _Listener] = {}
        self._sealed = False

    def open(
        self, attempt: ServiceAttempt, resources: ServiceResources, *, deadline: float
    ) -> HealthRelayEndpoint:
        if (
            attempt.worker_instance_id != self._owner
            or attempt.relay_start is None
            or not attempt.matches_review()
        ):
            raise ValueError("Relay requires the original reviewed start and daemon instance.")
        with self._lock:
            if (
                self._sealed or attempt.relay_job_id in self._listeners or len(self._listeners) >= 4
                or time.monotonic() >= deadline
            ):
                raise OSError("Relay is unavailable.")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            closing = threading.Event()
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen(4)
                listener.settimeout(0.1)
                port = listener.getsockname()[1]
                endpoint = HealthRelayEndpoint(owner_instance_id=self._owner, port=port)

                def serve() -> None:
                    with listener:
                        while not closing.is_set():
                            try:
                                client, _ = listener.accept()
                            except TimeoutError:
                                continue
                            except OSError:
                                return
                            with client:
                                try:
                                    _serve_request(
                                        client, port, closing,
                                        lambda end: self._probe(attempt, resources, end),
                                    )
                                except OSError:
                                    continue

                thread = threading.Thread(target=serve, name="jeff-service-health", daemon=False)
                thread.start()
            except BaseException:
                listener.close()
                raise
            self._listeners[attempt.relay_job_id] = _Listener(listener, closing, thread)
            return endpoint

    def close(self, start_job_id: UUID, *, deadline: float) -> None:
        with self._lock:
            entry = self._listeners.get(start_job_id)
            if entry is None:
                return
            entry.closing.set()
            entry.socket.close()
        entry.thread.join(max(0, deadline - time.monotonic()))
        if entry.thread.is_alive():
            raise TimeoutError("Relay shutdown is unconfirmed.")
        with self._lock:
            if self._listeners.get(start_job_id) is entry:
                del self._listeners[start_job_id]

    def seal(self) -> None:
        """Stop accepting and opening relays without waiting on in-flight probes."""
        with self._lock:
            self._sealed = True
            for entry in self._listeners.values():
                entry.closing.set()
                entry.socket.close()

    def close_all(self, *, deadline: float) -> None:
        self.seal()
        with self._lock:
            job_ids = tuple(self._listeners)
        unconfirmed = False
        for job_id in job_ids:
            try:
                self.close(job_id, deadline=deadline)
            except TimeoutError:
                unconfirmed = True
        if unconfirmed:
            raise TimeoutError("Relay shutdown is unconfirmed.")
