"""Bounded public collector seam: synthetic bytes only, no processes or persistence."""

import pytest
from pydantic import ValidationError

from jeff.application.execution import DockerBackendFault, DockerOutputSnapshot
from jeff.infrastructure.runtime_output import GuardedOutputCollector


def finish(collector: GuardedOutputCollector) -> None:
    collector.finish("stdout")
    collector.finish("stderr")


def test_public_output_is_escaped_and_sealed_only_after_real_eofs() -> None:
    collector = GuardedOutputCollector(1024)
    collector.accept("stdout", b"hello ")
    collector.accept("stdout", b"\xc3")
    collector.accept("stderr", b"note\n")
    collector.accept("stdout", b"\xa9\x1b[31m\t\r\n")
    collector.accept("stdout", "\u202e\U0001f600\n".encode("utf-8"))
    collector.accept("stderr", b"tail")
    with pytest.raises(DockerBackendFault):
        collector.seal()
    finish(collector)
    result = collector.seal()
    assert result.stdout == "hello \\u00e9\\u001b[31m\\t\\r\n\\u202e\\ud83d\\ude00\n"
    assert result.stderr == "note\ntail"
    assert result.complete and not result.aborted and result.guard_reason == "none"
    assert result.retained_bytes == len(result.stdout) + len(result.stderr)
    assert result.stdout_source_bytes == len(b"hello \xc3\xa9\x1b[31m\t\r\n") + len(
        "\u202e\U0001f600\n".encode("utf-8")
    )
    assert collector.buffered_bytes == 0 and collector.seal() is result


def test_sensitive_families_cross_every_chunk_boundary_and_latch_both_streams() -> None:
    examples = (
        b"password=SYNTHETIC_VALUE\n",
        b"MY_REFRESH_TOKEN = SYNTHETIC_VALUE\n",
        b'"api-key": "SYNTHETIC_VALUE"\n',
        b"AWS_SECRET_ACCESS_KEY=SYNTHETIC_VALUE\n",
        b"PRIVATE_KEY=SYNTHETIC_VALUE\n",
        b"Authorization: Basic SYNTHETIC_VALUE\n",
        b"Bearer SYNTHETIC_VALUE\n",
        b"postgres://reader:SYNTHETIC_VALUE@host/db\n",
        b"-----BEGIN OPENSSH PRIVATE KEY-----\nSYNTHETIC_VALUE\n",
    )
    for line in examples:
        for split in range(len(line) + 1):
            collector = GuardedOutputCollector(512)
            collector.accept("stdout", b"public prefix\n")
            collector.accept("stdout", line[:split])
            collector.accept("stdout", line[split:])
            collector.accept("stdout", b"later public output\n")
            collector.accept("stderr", b"later error output\n")
            finish(collector)
            result = collector.seal()
            assert result.complete and result.guard_reason == "sensitive_channel"
            assert result.stdout.startswith("public prefix\n")
            assert "SYNTHETIC_VALUE" not in result.stdout + result.stderr
            assert "later" not in result.stdout + result.stderr
            assert collector.buffered_bytes == 0
    collector = GuardedOutputCollector(1024)
    collector.accept("stdout", b"token_count=5\npassword prompt displayed\n")
    finish(collector)
    assert collector.seal().guard_reason == "none"


def test_intersecting_frames_and_eof_tails_are_not_released_prematurely() -> None:
    for prefix, suffix in (
        (b"pass", b"word=SYNTHETIC_VALUE\n"),
        (b"Authorization", b": Bearer SYNTHETIC_VALUE\n"),
    ):
        collector = GuardedOutputCollector(512)
        collector.accept("stdout", prefix)
        collector.accept("stderr", b"SYNTHETIC_VALUE\n")
        assert collector.buffered_bytes > 0
        collector.accept("stdout", suffix)
        finish(collector)
        result = collector.seal()
        assert result.guard_reason == "sensitive_channel" and result.complete
        assert "SYNTHETIC_VALUE" not in result.stdout + result.stderr
    collector = GuardedOutputCollector(512)
    collector.accept("stdout", b"toke")
    collector.finish("stdout")
    collector.accept("stderr", b"n=SYNTHETIC_VALUE")
    collector.finish("stderr")
    assert collector.seal().guard_reason == "sensitive_channel"
    collector = GuardedOutputCollector(512)
    collector.accept("stdout", b"public\n")
    collector.accept("stdout", b"unresolved SYNTHETIC_VALUE")
    collector.finish("stderr")
    aborted = collector.seal(abort=True)
    assert aborted.aborted and not aborted.complete and not aborted.stdout_eof
    assert (
        aborted.stdout.startswith("public\n")
        and "SYNTHETIC_VALUE" not in aborted.stdout + aborted.stderr
    )
    assert collector.buffered_bytes == 0


def test_raw_line_deferred_and_utf8_guards_have_exact_bounds() -> None:
    collector = GuardedOutputCollector(65536)
    collector.accept("stdout", b"a" * 8191 + b"\n")
    finish(collector)
    exact = collector.seal()
    assert exact.guard_reason == "none" and exact.retained_bytes == 8192
    for payload, reason in ((b"a" * 8192 + b"\n", "line_limit"), (b"bad\xff\n", "invalid_utf8")):
        collector = GuardedOutputCollector(65536)
        collector.accept("stdout", payload)
        collector.accept("stderr", b"later must be withheld\n")
        finish(collector)
        result = collector.seal()
        assert result.guard_reason == reason and result.complete
        assert "later" not in result.stdout + result.stderr and collector.buffered_bytes == 0
    collector = GuardedOutputCollector(65536)
    collector.accept("stdout", b"x")
    for _ in range(2):
        collector.accept("stderr", b"a\n" * 8191)
        assert collector.buffered_bytes <= 90112
    collector.accept("stderr", b"a\na")  # Exactly32768 deferred raw bytes remains allowed.
    assert collector.buffered_bytes > 0
    collector.accept("stderr", b"\n")
    finish(collector)
    result = collector.seal()
    assert result.guard_reason == "deferred_limit" and result.complete
    assert result.stdout == "" and "OUTPUT WITHHELD" in result.stderr


def test_shared_retained_cap_includes_escaped_text_and_notices_while_drain_continues() -> None:
    for limit in (1, 2, 16, 64):
        collector = GuardedOutputCollector(limit)
        collector.accept("stdout", b"abc\n" * 40)
        for _ in range(8):
            collector.accept("stderr", b"ignored after cap\n" * 512)
            assert collector.buffered_bytes == 0
        finish(collector)
        result = collector.seal()
        assert result.complete and result.truncated and result.guard_reason == "none"
        assert result.retained_bytes <= limit and result.retained_bytes == len(result.stdout) + len(
            result.stderr
        )
        assert "ignored" not in result.stdout + result.stderr
        assert result.stderr_source_bytes == len(b"ignored after cap\n" * 512) * 8
    collector = GuardedOutputCollector(64)
    collector.accept("stdout", b"a" * 20 + b"\n")
    collector.accept("stderr", b"b" * 20 + b"\n")
    collector.accept("stdout", b"c" * 30 + b"\n")
    finish(collector)
    shared = collector.seal()
    assert shared.truncated and shared.retained_bytes <= 64 and shared.stderr
    collector = GuardedOutputCollector(64)
    collector.accept("stdout", b"\0" * 32 + b"\n")
    finish(collector)
    escaped = collector.seal()
    assert escaped.truncated and escaped.retained_bytes <= 64
    assert all(char == "\n" or 32 <= ord(char) <= 126 for char in escaped.stdout + escaped.stderr)
    collector = GuardedOutputCollector(5)
    collector.accept("stdout", b"hello")
    finish(collector)
    exact = collector.seal()
    assert exact.stdout == "hello" and not exact.truncated


def test_lifecycle_misuse_and_descriptive_snapshot_cannot_assert_confirmation() -> None:
    for limit in (True, 0, 1048577):
        with pytest.raises(DockerBackendFault):
            GuardedOutputCollector(limit)
    collector = GuardedOutputCollector(64)
    with pytest.raises(DockerBackendFault):
        collector.accept("stdout", b"x" * 16385)
    collector.finish("stdout")
    with pytest.raises(DockerBackendFault):
        collector.accept("stdout", b"late")
    with pytest.raises(DockerBackendFault):
        collector.finish("stdout")
    with pytest.raises(DockerBackendFault):
        collector.seal()
    collector.finish("stderr")
    result = collector.seal()
    assert result.complete and result.retained_bytes == 0
    with pytest.raises(DockerBackendFault):
        collector.accept("stderr", b"after seal")
    assert not hasattr(result, "launch_claim") and not hasattr(result, "cleanup_verified")
    with pytest.raises(ValidationError):
        DockerOutputSnapshot.model_validate(
            {**result.model_dump(), "stdout": "\x1b", "retained_bytes": 1}
        )
    with pytest.raises(ValidationError):
        DockerOutputSnapshot.model_validate({**result.model_dump(), "stdout_eof": False})
