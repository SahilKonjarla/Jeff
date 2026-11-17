"""Original measured sandbox prefix and credential-excluding no-mutation snapshot."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path

from jeff.application.runtime import RuntimeLayout


def readonly_cli(layout: RuntimeLayout, env: dict[str, str], cwd: Path) -> list[str]:
    """Measure denials and the selected socket exception before observing doctor.

    Unchanged snapshots alone cannot establish that the harness blocked writes,
    live settings/DB reads and unrelated networking as intended.
    """
    profile = cwd.parent / "doctor.sb"
    profile.write_text(
        "(version 1)\n(allow default)\n(deny file-write*)\n"
        '(allow file-write* (literal "/dev/null"))\n(deny network*)\n'
        f"(allow network-outbound (literal {json.dumps(str(layout.socket))}))\n"
        f"(deny file-read-data (literal {json.dumps(str(layout.database))}) "
        f"(literal {json.dumps(str(layout.settings))}))\n"
    )
    prefix = ["/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, "-I", "-B"]
    control = subprocess.run(
        [
            *prefix,
            "-c",
            "import errno,socket,sys\n"
            'for path,mode in ((sys.argv[1],"x"),(sys.argv[2],"rb"),(sys.argv[3],"rb")):\n'
            " try:open(path,mode).close()\n"
            " except OSError as e:assert e.errno in (errno.EPERM,errno.EACCES)\n"
            ' else:raise SystemExit("read/write denial control failed")\n'
            "with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:s.connect(sys.argv[4])\n"
            "with socket.socket() as s:\n"
            " s.settimeout(1)\n"
            ' assert s.connect_ex(("127.0.0.1",9)) in (errno.EPERM,errno.EACCES)\n',
            str(cwd / "blocked"),
            str(layout.database),
            str(layout.settings),
            str(layout.socket),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert control.returncode == 0, control.stderr.decode(errors="replace")
    return prefix


def snapshot(layout: RuntimeLayout) -> dict[str, tuple[int, int, int, int, str | None]]:
    return {
        str(path): (
            path.lstat().st_ino,
            stat.S_IMODE(path.lstat().st_mode),
            path.lstat().st_mtime_ns,
            path.lstat().st_nlink,
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and path != layout.credential
            else None,
        )
        for root in (layout.data_dir, layout.runtime_dir)
        for path in (root, *root.rglob("*"))
    }
