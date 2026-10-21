"""Brief loopback-only service checks with owned-process teardown."""

import json
import os
import selectors
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scripts.create_demo_projects import generate


def test_real_health_endpoint_and_no_file_serving(tmp_path: Path) -> None:
    root = generate(tmp_path)[0].path
    process = subprocess.Popen(
        [sys.executable, "-B", "service.py", "--port", "0"],
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
            assert selector.select(timeout=5), "Demo service did not report readiness"
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
