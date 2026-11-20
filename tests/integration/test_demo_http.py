"""Brief loopback-only service checks with owned-process teardown."""

import json
import os
import runpy
import selectors
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

from scripts.create_demo_projects import generate


def test_real_health_endpoint_and_no_file_serving(tmp_path: Path) -> None:
    root = generate(tmp_path)[0].path
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            "import faulthandler; faulthandler.dump_traceback_later(4); "
            "import runpy; runpy.run_path('service.py', run_name='__main__')",
            "--port",
            "0",
        ],
        cwd=root,
        env={"PATH": os.defpath},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=5):
                process.terminate()
                try:
                    _, stderr = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _, stderr = process.communicate(timeout=3)
                pytest.fail(
                    f"Demo service did not report readiness; child exit={process.returncode}; "
                    f"startup stderr/traceback:\n{stderr[-8192:]}"
                )
            ready = json.loads(process.stdout.readline(1024))
        assert ready["host"] == "127.0.0.1" and 0 < ready["port"] <= 65535
        url = f"http://127.0.0.1:{ready['port']}/health"
        assert ready["url"] == url
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=2) as response:
            assert response.status == 200
            assert json.load(response) == {"status": "ok", "service": "jeff-demo"}
        with pytest.raises(urllib.error.HTTPError) as caught:
            opener.open(url.replace("/health", "/service.py"), timeout=2)
        assert caught.value.code == 404
        caught.value.close()
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    assert process.poll() is not None


def test_numeric_binding_does_not_require_reverse_dns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = generate(tmp_path)[0].path
    service = runpy.run_path(str(root / "service.py"))

    def refuse_dns(host: str = "") -> str:
        raise AssertionError("Numeric demo binding must not perform reverse DNS")

    monkeypatch.setattr(socket, "getfqdn", refuse_dns)
    monkeypatch.setattr(HTTPServer, "serve_forever", lambda self: None)
    monkeypatch.setattr(sys, "argv", ["service.py", "--port", "0"])
    assert service["main"]() == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["host"] == "127.0.0.1" and 0 < ready["port"] <= 65535


def test_port_collision_is_actionable_and_invalid_port_is_rejected(tmp_path: Path) -> None:
    root = generate(tmp_path)[0].path
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        result = subprocess.run(
            [sys.executable, "-B", "service.py", "--port", str(occupied.getsockname()[1])],
            cwd=root,
            env={"PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 1 and "Cannot bind" in result.stderr
    invalid = subprocess.run(
        [sys.executable, "-B", "service.py", "--port", "65536"],
        cwd=root,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert invalid.returncode == 2 and "between 0 and 65535" in invalid.stderr
