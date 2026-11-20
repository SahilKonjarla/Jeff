"""Bounded, read-only live log observations; offsets are not job authority."""

from threading import Lock
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from ._model import _Model

LOG_TAIL_BYTES = 64 * 1024
LogStream = Literal["stdout", "stderr"]


class LiveLogChunk(_Model):
    stream: LogStream
    text: str = Field(min_length=1, max_length=LOG_TAIL_BYTES)


class LiveLogPage(_Model):
    offset: StrictInt = Field(ge=0)
    next_offset: StrictInt = Field(ge=0)
    dropped: StrictBool
    closed: StrictBool
    chunks: tuple[LiveLogChunk, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        size = sum(len(chunk.text) for chunk in self.chunks)
        if (
            self.next_offset - self.offset != size
            or size > LOG_TAIL_BYTES
            or any(
                char != "\n" and not 32 <= ord(char) <= 126
                for chunk in self.chunks
                for char in chunk.text
            )
        ):
            raise ValueError("Invalid sanitized log page.")
        return self


class LiveLogTail:
    """Copies only already-sanitized bytes; never waits for a log consumer."""

    def __init__(self, limit: int = LOG_TAIL_BYTES) -> None:
        if not 0 < limit <= LOG_TAIL_BYTES:
            raise ValueError("Invalid live log limit.")
        self._limit = limit
        self._lock = Lock()
        self._text = bytearray()
        self._streams = bytearray()
        self._offset = 0
        self._closed = False

    def publish(self, text: bytes, streams: bytes) -> None:
        """Accept adjacent sanitized bytes tagged 0 for stdout or 1 for stderr."""
        if not text:
            return
        if (
            len(text) != len(streams)
            or any(c != 10 and not 32 <= c <= 126 for c in text)
            or any(tag > 1 for tag in streams)
        ):
            raise ValueError("Invalid live log input.")
        with self._lock:
            if self._closed:
                return
            if len(text) >= self._limit:
                self._offset += len(self._text) + len(text) - self._limit
                self._text[:] = text[-self._limit :]
                self._streams[:] = streams[-self._limit :]
                return
            drop = max(0, len(self._text) + len(text) - self._limit)
            if drop:
                del self._text[:drop]
                del self._streams[:drop]
                self._offset += drop
            self._text.extend(text)
            self._streams.extend(streams)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def read(self, after: int | None = None) -> LiveLogPage:
        with self._lock:
            end = self._offset + len(self._text)
            if after is not None and not 0 <= after <= end:
                raise ValueError("Invalid live log cursor.")
            begin = self._offset if after is None else max(after, self._offset)
            index = begin - self._offset
            text = bytes(self._text[index:])
            streams = bytes(self._streams[index:])
            dropped = self._offset > (0 if after is None else after)
            closed = self._closed
        # Group adjacent bytes, capping object count even with adversarial interleaving.
        # In that case the page advances only as far as the bytes it actually returns.
        chunks: list[LiveLogChunk] = []
        start = 0
        while start < len(text) and len(chunks) < 128:
            tag = streams[start]
            stop = streams.find(bytes((1 - tag,)), start)
            if stop == -1:
                stop = len(text)
            chunks.append(
                LiveLogChunk(
                    stream="stdout" if tag == 0 else "stderr", text=text[start:stop].decode()
                )
            )
            start = stop
        return LiveLogPage(
            offset=begin,
            next_offset=begin + start,
            dropped=dropped,
            closed=closed and start == len(text),
            chunks=tuple(chunks),
        )
