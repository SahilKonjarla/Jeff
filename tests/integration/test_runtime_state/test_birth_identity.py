"""Bounded exact-birth sampling and optional actual compiled SDK-oracle comparison."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from jeff.application.runtime import ControlFault, RuntimeLayout
from jeff.infrastructure.runtime_state import Ownership


@pytest.mark.parametrize("mutation", ["directory_content", "directory_mode", "file_content"])
def test_exact_birth_sampling_handles_only_benign_directory_metadata_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from jeff.infrastructure import runtime_state

    target = tmp_path / "sample"
    if mutation == "file_content":
        target.write_bytes(b"a")
        target.chmod(0o600)
    else:
        target.mkdir(mode=0o700)
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    original = os.fstat
    changed = False
    try:
        expected = runtime_state._native_identity(fd)

        def sample(descriptor: int) -> os.stat_result:
            nonlocal changed
            before = original(descriptor)
            if descriptor == fd and not changed:
                changed = True
                if mutation == "directory_content":
                    (target / "created").write_bytes(b"")
                elif mutation == "directory_mode":
                    target.chmod(0o755)
                else:
                    target.write_bytes(b"ab")
            return before

        monkeypatch.setattr(os, "fstat", sample)
        if mutation == "directory_content":
            assert runtime_state._native_identity(fd) == expected
            calls = 0

            def changing(descriptor: int) -> os.stat_result:
                nonlocal calls
                before = original(descriptor)
                if descriptor == fd:
                    calls += 1
                    (target / f"later-{calls}").write_bytes(b"")
                    if calls > 1:
                        return original(descriptor)
                return before

            monkeypatch.setattr(os, "fstat", changing)
            with pytest.raises(ControlFault) as unstable:
                runtime_state._native_identity(fd)
            assert unstable.value.code == "runtime_ownership_unconfirmed"
            assert calls == 2  # One fresh observation, never an unbounded retry loop.
        else:
            with pytest.raises(ControlFault) as raised:
                runtime_state._native_identity(fd)
            assert raised.value.code == "runtime_ownership_unconfirmed"
        assert changed
    finally:
        os.close(fd)


def test_published_receipt_birth_matches_compiled_sdk_oracle(
    layout: RuntimeLayout,
    recorded: tuple[Ownership, bytes],
) -> None:
    oracle = os.environ.get("JEFF_TEST_STAT_ORACLE")
    if oracle is None:
        pytest.skip("Compiled SDK oracle required; mandatory in guarded acceptance")
    result = subprocess.run([oracle, str(layout.endpoint_receipt)], capture_output=True, timeout=5)
    assert result.returncode == 0
    expected = json.loads(result.stdout)
    actual = json.loads(layout.endpoint_receipt.read_bytes())["bindings"]["receipt"]
    assert actual["birth_ns"] == expected["birth_s"] * 1000000000 + expected["birth_ns"]
    assert actual["device"] == expected["dev"] and actual["inode"] == expected["ino"]
