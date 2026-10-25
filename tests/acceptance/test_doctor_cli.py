"""Actual read-only doctor entry point; selected absent installation stays absent."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path


def test_doctor_absent_installation_is_read_only_outside_git(tmp_path: Path) -> None:
    homes = [tmp_path / name for name in ("home", "tmp", "config", "cache", "data", "foreign")]
    for path in homes:
        path.mkdir(mode=0o700)
    data, runtime = tmp_path / "d", tmp_path / "r"
    env = {
        "PATH": os.defpath,
        "HOME": str(homes[0]),
        "TMPDIR": str(homes[1]),
        "XDG_CONFIG_HOME": str(homes[2]),
        "XDG_CACHE_HOME": str(homes[3]),
        "XDG_DATA_HOME": str(homes[4]),
        "JEFF_DATA_DIR": str(data),
        "JEFF_RUNTIME_DIR": str(runtime),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }
    negative = subprocess.run(
        ["/usr/bin/git", "-C", str(homes[-1]), "rev-parse", "--show-toplevel"],
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert negative.returncode == 128 and b"not a git repository" in negative.stderr
    profile = tmp_path / "readonly.sb"
    profile.write_text(
        "(version 1)\n(allow default)\n(deny file-write*)\n(deny network*)\n"
        '(allow file-write* (literal "/dev/null"))\n'
    )
    prefix = ["/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, "-I", "-B"]
    denied = homes[0] / "blocked"
    control = subprocess.run(
        [
            *prefix,
            "-c",
            'import errno,sys\ntry:open(sys.argv[1],"x").close()\n'
            "except OSError as e:assert e.errno in (errno.EPERM,errno.EACCES)\n"
            'else:raise SystemExit("read-only control failed")',
            str(denied),
        ],
        cwd=homes[-1],
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert control.returncode == 0 and not denied.exists()

    def snapshot() -> dict[str, tuple[int, int, int, int]]:
        return {
            str(path): (
                path.lstat().st_ino,
                stat.S_IMODE(path.lstat().st_mode),
                path.lstat().st_mtime_ns,
                path.lstat().st_nlink,
            )
            for home in homes
            for path in (home, *home.rglob("*"))
        }

    before = snapshot()
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=homes[-1],
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["error"] is None
    report = payload["data"]
    assert report["summary"] == "uninitialized" and report["runtime"] is None
    assert [check["name"] for check in report["checks"]] == [
        "platform",
        "python",
        "git",
        "paths",
        "runtime",
        "settings",
        "storage",
    ]
    for check in report["checks"][:3]:
        assert check["status"] == "ok" and check["code"] is None
        assert check["required"] is True
    for check in report["checks"][3:]:
        assert check["status"] == "missing" and check["code"] == "installation_absent"
        assert check["required"] is False
    assert {entry["role"] for entry in report["paths"]["entries"]} == {
        "data_dir",
        "runtime_dir",
        "database",
        "settings",
        "credential",
        "socket",
        "lock",
    }
    assert all(entry["state"] == "missing" for entry in report["paths"]["entries"])
    assert isinstance(report["python_version"], str) and 0 < len(report["python_version"]) <= 32
    assert isinstance(report["git_version"], str) and 0 < len(report["git_version"]) <= 64
    assert not data.exists() and not runtime.exists()
    assert snapshot() == before
