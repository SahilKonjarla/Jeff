"""Memory-only guarded output. No credential discovery, persistence or completion release.

Filtering is pattern-based, not universal secret detection. After a guard/cap, raw
bytes are counted and discarded while the transport continues its finite draining.
"""

import codecs
import json
from typing import Literal

from jeff.application.execution import DockerBackendFault, DockerOutputSnapshot, OutputGuardReason

_LINE_BYTES = 8192  # Raw bytes, including LF; exactly8192 is allowed.
_DEFERRED_BYTES = 32768
_CHUNK_BYTES = 16384
_INT64_MAX = 9223372036854775807
_KEYS = (
    b"password",
    b"passwd",
    b"token",
    b"secret",
    b"credential",
    b"credentials",
    b"api_key",
    b"apikey",
    b"access_key",
    b"private_key",
    b"privatekey",
    b"authorization",
    b"auth",
)


def _identifier(value: int) -> bool:
    return 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122 or value in (45, 95)


def _sensitive(line: bytearray) -> bool:
    """Bounded linear scans with fixed keywords; never decode combined-channel UTF-8."""
    raw = bytes(line).lower()
    if b"-----begin " in raw and b"private key" in raw:
        return True
    for token in raw.split():
        if b"://" in token and b"@" in token.split(b"://", 1)[1].split(b"/", 1)[0]:
            return True
    index = 0
    while index < len(raw):
        if not _identifier(raw[index]):
            index += 1
            continue
        start = index
        while index < len(raw) and _identifier(raw[index]):
            index += 1
        word = raw[start:index].replace(b"-", b"_").strip(b"_")
        if word == b"bearer" and index < len(raw) and raw[index] in b" \t\r\n":
            return True
        if not any(word == key or word.endswith(b"_" + key) for key in _KEYS):
            continue
        after = index
        if after < len(raw) and raw[after] in b"\"'":
            after += 1
        while after < len(raw) and raw[after] in b" \t\r":
            after += 1
        if after < len(raw) and raw[after] in b":=":
            return True
    return False


def _escaped(char: str) -> bytes:
    if char == "\n" or 32 <= ord(char) <= 126:
        return char.encode("ascii")
    return json.dumps(char, ensure_ascii=True)[1:-1].encode("ascii")


class GuardedOutputCollector:
    """One attach-reader's bounded sink; callbacks perform no filesystem/network I/O.

    A checkpoint releases bytes only when every intersecting per-stream and
    observed-arrival frame is classified. Deferred raw data and tags are bounded;
    arbitrary cross-stream ordering/obfuscation is not universal redaction proof.
    """

    def __init__(self, retention_limit: int) -> None:
        if type(retention_limit) is not int or not 1 <= retention_limit <= 1048576:
            raise DockerBackendFault("docker_unsafe_configuration")
        self._limit = retention_limit
        self._text = [bytearray(), bytearray()]
        self._size = 0
        self._source = [0, 0]
        self._eof = [False, False]
        self._lines = [bytearray(), bytearray()]
        self._combined = bytearray()
        self._window = bytearray()
        self._tags = bytearray()
        self._guard: OutputGuardReason = "none"
        self._truncated = False
        self._snapshot: DockerOutputSnapshot | None = None

    @property
    def retention_limit(self) -> int:
        return self._limit

    @property
    def buffered_bytes(self) -> int:
        """Current raw/tag byte storage, excluding retained text and Python overhead."""
        return (
            len(self._combined) + sum(map(len, self._lines)) + len(self._window) + len(self._tags)
        )

    @staticmethod
    def _channel(stream: Literal["stdout", "stderr"]) -> int:
        if stream not in ("stdout", "stderr"):
            raise DockerBackendFault("docker_output_failed")
        return 0 if stream == "stdout" else 1

    def _drop_pending(self) -> None:
        self._combined.clear()
        self._window.clear()
        self._tags.clear()
        for line in self._lines:
            line.clear()

    def _notice(self, channel: int, message: bytes) -> None:
        marker = message[: self._limit]
        remove = max(0, self._size + len(marker) - self._limit)
        if remove:
            self._truncated = True
        for index in (channel, 1 - channel):
            count = min(remove, len(self._text[index]))
            if count:
                del self._text[index][-count:]
                self._size -= count
                remove -= count
        self._text[channel].extend(marker)
        self._size += len(marker)
        self._drop_pending()

    def _withhold(self, channel: int, reason: OutputGuardReason) -> None:
        if self._guard != "none" or self._truncated:
            return
        self._guard = reason
        self._notice(channel, b"[OUTPUT WITHHELD]\n")

    def _classify(self, line: bytearray, channel: int, *, decode: bool) -> bool:
        if _sensitive(line):
            self._withhold(channel, "sensitive_channel")
            return False
        if decode:
            try:
                line.decode("utf-8", "strict")
            except UnicodeError:
                self._withhold(channel, "invalid_utf8")
                return False
        return True

    def _release(self) -> None:
        # UTF-8 may span arrival runs belonging to different streams. Decode each
        # stream incrementally only after all participating frames passed.
        decoders = [codecs.getincrementaldecoder("utf-8")("strict") for _ in range(2)]
        for value, channel in zip(self._window, self._tags, strict=True):
            for char in decoders[channel].decode(bytes((value,)), final=False):
                encoded = _escaped(char)
                if self._size + len(encoded) > self._limit:
                    self._truncated = True
                    self._notice(channel, b"[OUTPUT TRUNCATED]\n")
                    return
                self._text[channel].extend(encoded)
                self._size += len(encoded)
        for decoder in decoders:
            if decoder.decode(b"", final=True):
                raise DockerBackendFault("docker_output_failed")
        self._window.clear()
        self._tags.clear()

    def accept(self, stream: Literal["stdout", "stderr"], data: bytes) -> None:
        channel = self._channel(stream)
        if (
            self._snapshot is not None
            or self._eof[channel]
            or not isinstance(data, bytes)
            or len(data) > _CHUNK_BYTES
        ):
            raise DockerBackendFault("docker_output_failed")
        self._source[channel] = min(_INT64_MAX, self._source[channel] + len(data))
        if not data or self._guard != "none" or self._truncated:
            return
        if self._size == self._limit:
            self._truncated = True
            self._notice(channel, b"[OUTPUT TRUNCATED]\n")
            return
        for value in data:
            self._window.append(value)
            self._tags.append(channel)
            self._combined.append(value)
            self._lines[channel].append(value)
            if len(self._window) > _DEFERRED_BYTES:
                self._withhold(channel, "deferred_limit")
                return
            if len(self._combined) > _LINE_BYTES or len(self._lines[channel]) > _LINE_BYTES:
                self._withhold(channel, "line_limit")
                return
            if value == 10:
                if not self._classify(self._combined, channel, decode=False):
                    return
                if not self._classify(self._lines[channel], channel, decode=True):
                    return
                self._combined.clear()
                self._lines[channel].clear()
                if not any(self._lines):
                    self._release()
                    if self._truncated:
                        return

    def finish(self, stream: Literal["stdout", "stderr"]) -> None:
        channel = self._channel(stream)
        if self._snapshot is not None or self._eof[channel]:
            raise DockerBackendFault("docker_output_failed")
        self._eof[channel] = True
        if self._guard != "none" or self._truncated:
            return
        if not self._classify(self._lines[channel], channel, decode=True):
            return
        self._lines[channel].clear()
        # An opposite-stream EOF alone cannot flush the combined pending frame.
        if all(self._eof):
            if not self._classify(self._combined, channel, decode=False):
                return
            self._combined.clear()
            self._release()

    def seal(self, *, abort: bool = False) -> DockerOutputSnapshot:
        if self._snapshot is not None:
            return self._snapshot
        if not abort and not all(self._eof):
            raise DockerBackendFault("docker_output_failed")
        if abort:
            if self._guard == "none" and not self._truncated:
                self._notice(1, b"[OUTPUT INCOMPLETE]\n")
            self._drop_pending()
        result = DockerOutputSnapshot(
            retention_limit=self._limit,
            stdout=self._text[0].decode("ascii"),
            stderr=self._text[1].decode("ascii"),
            retained_bytes=self._size,
            stdout_source_bytes=self._source[0],
            stderr_source_bytes=self._source[1],
            stdout_eof=self._eof[0],
            stderr_eof=self._eof[1],
            aborted=abort,
            truncated=self._truncated,
            guard_reason=self._guard,
        )
        self._snapshot = result
        self._text[0].clear()
        self._text[1].clear()
        self._drop_pending()
        return result
