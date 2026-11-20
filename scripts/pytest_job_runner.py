"""Slice 1 container entry point: stdout is one report; pytest output is stderr.

Called as `python -I -B /opt/jeff/pytest_job_runner.py RUN_TOKEN` from the prepared
project root. The token correlates the output with its approved job, not with a
trusted author. This runner must not be used to authorize any job or assert
controller completion.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import BinaryIO

_REPORT_LIMIT = 64 * 1024
_FAILURE_LIMIT = 128


class _Reports:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[dict[str, str | int]] = []
        self.unsupported = False

    def pytest_runtest_logreport(self, report: object) -> None:
        # Kept local so a missing pytest can produce a useful runner diagnostic.
        from pytest import TestReport

        if not isinstance(report, TestReport):
            self.unsupported = True
            return
        if report.passed and report.when == "call":
            self.passed += 1
        if not report.failed:
            return
        if report.when != "call" or len(self.failures) >= _FAILURE_LIMIT:
            self.unsupported = True
            return
        path, zero_based_line, _ = report.location
        if zero_based_line is None:
            self.unsupported = True
            return
        excerpt = report.longreprtext[:2048].expandtabs(4)[:2048]
        self.failures.append(
            {
                "node_id": report.nodeid,
                "path": path,
                "line": zero_based_line + 1,
                "excerpt": "".join(
                    char if char.isprintable() or char == "\n" else "?" for char in excerpt
                ),
            }
        )

    def emit(self, output: BinaryIO, token: str, code: int) -> None:
        if self.unsupported or code not in (0, 1):
            return
        payload = json.dumps(
            {
                "version": 1,
                "run_token": token,
                "passed": self.passed,
                "failed": len(self.failures),
                "exit_status": code,
                "failures": self.failures,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) <= _REPORT_LIMIT:
            output.write(payload)
        else:
            print("Jeff pytest runner: report exceeds size limit.", file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or re.fullmatch(r"[0-9a-f]{32}", argv[0]) is None:
        print("Jeff pytest runner: invalid run token.", file=sys.stderr)
        return 4
    sys.stdout.flush()
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as report_output:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        try:
            import pytest
        except ImportError:
            print(
                "Jeff pytest runner: pytest is unavailable in the declared image.", file=sys.stderr
            )
            return 4
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
            os.environ.pop(key, None)
        root = Path.cwd()
        plugin = _Reports()
        code = int(
            pytest.main(
                [
                    "--rootdir",
                    str(root),
                    "-p",
                    "no:cacheprovider",
                    "--color=no",
                    "--tb=short",
                    "-q",
                    ".",
                ],
                plugins=[plugin],
            )
        )
        plugin.emit(report_output, argv[0], code)
        return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
