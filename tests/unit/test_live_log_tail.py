"""Live tail uses the output privacy boundary and is independent of archive capacity."""

import pytest

from jeff.application.execution.log_tail import LOG_TAIL_BYTES, LiveLogPage, LiveLogTail
from jeff.infrastructure.runtime_output import GuardedOutputCollector, PytestOutputCollector


def _text(page: LiveLogPage) -> str:
    return "".join(chunk.text for chunk in page.chunks)


def test_initial_tail_and_reconnect_are_byte_bounded() -> None:
    tail = LiveLogTail()
    tail.publish(b"a" * LOG_TAIL_BYTES, bytes(LOG_TAIL_BYTES))
    assert not tail.read().dropped
    tail.publish(b"b", b"\x01")
    page = tail.read()
    assert page.offset == 1 and page.dropped
    assert _text(page) == "a" * (LOG_TAIL_BYTES - 1) + "b"
    assert page.chunks[-1].stream == "stderr"
    tail.publish(b"c", b"\x00")
    resumed = tail.read(after=page.next_offset)
    assert not resumed.dropped and _text(resumed) == "c"
    assert tail.read(after=0).dropped
    with pytest.raises(ValueError):
        tail.read(after=resumed.next_offset + 1)


def test_stream_interleaving_pages_without_skipping_bytes() -> None:
    tail = LiveLogTail()
    tail.publish(b"ab" * 128, b"\x00\x01" * 128)
    tail.close()
    first = tail.read()
    second = tail.read(first.next_offset)
    assert not first.closed and second.closed
    assert first.next_offset == second.offset == 128 and second.next_offset == 256
    assert _text(first) + _text(second) == "ab" * 128


def test_live_filter_keeps_following_after_archive_truncation() -> None:
    capture = GuardedOutputCollector(8)
    tail = LiveLogTail()
    capture.follow_into(tail)
    capture.accept("stdout", b"first\n")
    checkpoint = tail.read()
    capture.accept("stderr", b"second\n")
    capture.accept("stdout", b"third\n")
    resumed = tail.read(checkpoint.next_offset)
    assert _text(resumed) == "second\nthird\n"
    capture.finish("stdout")
    capture.finish("stderr")
    assert capture.seal().truncated
    assert tail.read(resumed.next_offset).closed


def test_live_filter_does_not_release_pending_or_sensitive_bytes() -> None:
    capture = GuardedOutputCollector(8)
    tail = LiveLogTail()
    capture.follow_into(tail)
    capture.accept("stdout", b"ordinary public output\n")
    cursor = tail.read().next_offset
    capture.accept("stdout", b"to")
    assert not tail.read(cursor).chunks
    capture.accept("stderr", b"ken: synthetic\n")
    capture.accept("stdout", b"not after withholding\n")
    assert _text(tail.read(cursor)) == "[OUTPUT WITHHELD]\n"
    capture.seal(abort=True)
    assert tail.read().closed


def test_live_pytest_output_excludes_the_report_channel() -> None:
    capture = PytestOutputCollector(64, run_token="ab" * 16, approved_paths=())
    tail = LiveLogTail()
    capture.follow_into(tail)
    capture.accept("stdout", b"structured report")
    capture.accept("stderr", b"pytest output\n")
    capture.finish("stdout")
    capture.finish("stderr")
    capture.seal()
    assert _text(tail.read()) == "pytest output\n"
