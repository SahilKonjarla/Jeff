"""Fixed Compose log target, bounded capture, and sanitized public output."""

import json
import os
import time
from pathlib import Path

import pytest

from jeff.application.compose_status import COMPOSE_LOG_LIMIT
from jeff.application.execution import DockerBackendFault
from jeff.infrastructure.docker_cli import compose, transport

_ROOT = Path("/sandbox-project")
_ENDPOINT = "unix:///test/docker.sock"
_CID = "a" * 64


def _row(cid: str = _CID, root: Path = _ROOT) -> bytes:
    return json.dumps(
        [cid, "sandbox-project", "api", str(root), str(root / "compose.yaml"), "False", "running"]
    ).encode()


@pytest.mark.parametrize(
    "listing", [b"", _row() + b"\n" + _row("b" * 64), _row(root=Path("/other"))]
)
def test_logs_refuse_without_exclusive_fixture_match(
    monkeypatch: pytest.MonkeyPatch, listing: bytes
) -> None:
    cli = object.__new__(compose.ComposeInspectionCLI)
    monkeypatch.setattr(cli, "check", lambda: None)
    monkeypatch.setattr(cli, "_socket", lambda _: (1,))
    monkeypatch.setattr(cli, "_control", lambda *args, **kwargs: listing)

    def forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("Read dispatched for an unconfirmed target")

    monkeypatch.setattr(cli, "_capture", forbidden)
    with pytest.raises(DockerBackendFault, match="docker_target_mismatch"):
        cli.compose_logs(
            endpoint=_ENDPOINT, project="sandbox-project", service="api", root=_ROOT,
            deadline=time.monotonic() + 5,
        )


@pytest.mark.parametrize("mode", ["safe", "secret", "oversized", "command_failed"])
def test_log_capture_with_real_pipes_and_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    cli = object.__new__(compose.ComposeInspectionCLI)
    for attr in ("binary", "config", "home"):
        monkeypatch.setattr(cli, attr, tmp_path / attr, raising=False)
    monkeypatch.setattr(cli, "check", lambda: None)
    monkeypatch.setattr(cli, "_socket", lambda _: (1,))
    monkeypatch.setattr(cli, "_control", lambda *args, **kwargs: _row())
    assert COMPOSE_LOG_LIMIT == transport.COMPOSE_LOG_LIMIT == 16384
    if mode == "oversized":
        monkeypatch.setattr(transport, "COMPOSE_LOG_LIMIT", 8)

    def pipe(data: bytes):
        read, write = os.pipe()
        try:
            os.write(write, data)
        finally:
            os.close(write)
        return os.fdopen(read, "rb")

    class Captured:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            assert argv[-6:] == ["container", "logs", "--tail", "40", "--", _CID]
            assert "--follow" not in argv
            assert kwargs["env"] == cli._environment()
            self.stdout = pipe(b"public ready\n")
            self.stderr = pipe(b"token=synthetic-value\n" if mode == "secret" else b"\x1b[0mnote\n")

        def poll(self) -> int:
            return 1 if mode == "command_failed" else 0

        def wait(self, timeout: float) -> int:
            return self.poll()

    monkeypatch.setattr(transport.subprocess, "Popen", Captured)

    def logs():
        return cli.compose_logs(
            endpoint=_ENDPOINT, project="sandbox-project", service="api", root=_ROOT,
            deadline=time.monotonic() + 5,
        )

    if mode == "command_failed":
        with pytest.raises(DockerBackendFault, match="docker_command_failed"):
            logs()
        return
    report = logs()
    if mode == "safe":
        assert report.withheld is None and report.stdout == "public ready\n"
        assert report.stderr == "\\u001b[0mnote\n"
    else:
        assert report.withheld == ("sanitizer" if mode == "secret" else "output_limit")
        assert not report.stdout and not report.stderr
