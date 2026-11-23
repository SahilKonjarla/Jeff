import json
import subprocess
import sys
from pathlib import Path

import pytest

from jeff.application.execution import DockerJobOutcome
from jeff.application.execution.pytest_report import (
    PYTEST_REPORT_BYTES,
    PytestReportBuffer,
    read_pytest_report,
)
from jeff.infrastructure.runtime_output import PytestOutputCollector

_TOKEN = "ab" * 16
_PATH = "test_service.py"


def _payload(**changes: object) -> bytes:
    record = {
        "version": 1,
        "run_token": _TOKEN,
        "passed": 1,
        "failed": 1,
        "exit_status": 1,
        "failures": [
            {"node_id": f"{_PATH}::test_add", "path": _PATH, "line": 12, "excerpt": "assert 0"}
        ],
    }
    return json.dumps(record | changes).encode()


def test_reads_a_matching_failure_report() -> None:
    status, result = read_pytest_report(
        _payload(), run_token=_TOKEN, exit_status=1, approved_paths={_PATH}
    )
    assert status == "available"
    assert result is not None
    assert (result.passed, result.failed, result.exit_status) == (1, 1, 1)
    assert result.failures[0].line == 12
    assert result.failures[0].excerpt == "assert 0"


def test_reads_an_all_passing_report() -> None:
    status, result = read_pytest_report(
        _payload(failed=0, exit_status=0, failures=[]),
        run_token=_TOKEN,
        exit_status=0,
        approved_paths={_PATH},
    )
    assert status == "available"
    assert result is not None and result.failures == ()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", "missing"),
        (b"x" * (PYTEST_REPORT_BYTES + 1), "oversized"),
        (b"{", "invalid"),
        (b"\xff", "invalid"),
        (b'{"version":1,"version":1}', "invalid"),
        (_payload(version=True), "invalid"),
        (_payload(run_token="cd" * 16), "invalid"),
        (_payload(exit_status=0), "invalid"),
        (_payload(failed=2), "invalid"),
    ],
)
def test_rejects_bad_reports(payload: bytes, expected: str) -> None:
    assert read_pytest_report(payload, run_token=_TOKEN, exit_status=1, approved_paths={_PATH}) == (
        expected,
        None,
    )


@pytest.mark.parametrize(
    "path", ["../test_service.py", "/test_service.py", "other.py", "./test_service.py"]
)
def test_reports_must_refer_to_canonical_approved_project_paths(path: str) -> None:
    payload = _payload(
        failures=[{"node_id": f"{path}::test", "path": path, "line": 1, "excerpt": ""}]
    )
    assert read_pytest_report(payload, run_token=_TOKEN, exit_status=1, approved_paths={_PATH}) == (
        "invalid",
        None,
    )


def test_buffer_does_not_parse_an_incomplete_stream() -> None:
    sink = PytestReportBuffer()
    sink.accept(_payload())
    assert sink.finish(complete=False, run_token=_TOKEN, exit_status=1, approved_paths={_PATH}) == (
        "invalid",
        None,
    )


def test_buffer_discards_oversized_report_even_if_prefix_is_valid() -> None:
    sink = PytestReportBuffer()
    payload = _payload()
    sink.accept(payload)
    sink.accept(b" " * (PYTEST_REPORT_BYTES - len(payload)))
    sink.accept(b" ")
    sink.accept(payload)
    assert sink.finish(complete=True, run_token=_TOKEN, exit_status=1, approved_paths={_PATH}) == (
        "oversized",
        None,
    )


def test_buffer_accepts_fragmented_report_at_byte_limit() -> None:
    sink = PytestReportBuffer()
    payload = _payload()
    sink.accept(payload[:20])
    sink.accept(payload[20:])
    sink.accept(b" " * (PYTEST_REPORT_BYTES - len(payload)))
    status, report = sink.finish(
        complete=True, run_token=_TOKEN, exit_status=1, approved_paths={_PATH}
    )
    assert status == "available" and report is not None


_RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "pytest_job_runner.py"


def _run_fixture(
    root: Path, *flags: str, selections: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", *flags, str(_RUNNER), _TOKEN, *selections],
        cwd=root,
        env={},
        capture_output=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("failing", [True, False])
def test_runner_separates_report_from_real_pytest_output(tmp_path: Path, failing: bool) -> None:
    (tmp_path / _PATH).write_text(
        "import os\n"
        "os.write(1, b'collection output\\n')\n"
        "def test_pass():\n"
        "    assert 1\n"
        "def test_fixture():\n"
        f"    assert {not failing}\n",
        encoding="utf-8",
    )
    run = _run_fixture(tmp_path)
    assert run.returncode == int(failing), run.stderr
    status, report = read_pytest_report(
        run.stdout, run_token=_TOKEN, exit_status=run.returncode, approved_paths={_PATH}
    )
    assert status == "available" and report is not None
    assert (report.passed, report.failed) == ((1, 1) if failing else (2, 0))
    assert b"passed" in run.stderr
    assert b"collection output" not in run.stdout
    if failing:
        assert report.failures[0].node_id == f"{_PATH}::test_fixture"
        assert report.failures[0].line == 5
        assert "assert False" in report.failures[0].excerpt


def test_runner_reports_missing_pytest_without_emitting_a_report(tmp_path: Path) -> None:
    run = _run_fixture(tmp_path, "-S")
    assert run.returncode == 4
    assert run.stdout == b""
    assert b"pytest is unavailable" in run.stderr


def test_runner_executes_only_exact_failed_node(tmp_path: Path) -> None:
    (tmp_path / _PATH).write_text(
        "import pytest\n"
        "def test_other():\n    raise AssertionError('must not run')\n"
        "@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_case(n):\n    assert n != 2\n",
        encoding="utf-8",
    )
    selected = f"{_PATH}::test_case[2]"
    run = _run_fixture(tmp_path, selections=(selected,))
    assert run.returncode == 1, run.stderr
    status, result = read_pytest_report(
        run.stdout, run_token=_TOKEN, exit_status=1, approved_paths={_PATH}
    )
    assert status == "available" and result is not None and result.version == 2
    assert result.passed == 0 and [f.node_id for f in result.failures] == [selected]
    group = _run_fixture(tmp_path, selections=(f"{_PATH}::test_case",))
    assert group.returncode == 4 and not group.stdout
    assert b"collection does not match exact selection" in group.stderr


def _collector(payload: bytes, log: bytes = b"pytest summary\n") -> PytestOutputCollector:
    capture = PytestOutputCollector(256, run_token=_TOKEN, approved_paths={_PATH})
    capture.accept("stdout", payload)
    capture.accept("stderr", log)
    capture.finish("stdout")
    capture.finish("stderr")
    return capture


def test_pytest_capture_never_includes_report_in_job_logs() -> None:
    payload = _payload()
    capture = _collector(payload)
    output = capture.seal()
    assert output.stdout == "" and output.stdout_source_bytes == len(payload)
    assert output.stderr == "pytest summary\n"
    outcome = DockerJobOutcome(exit_code=1, cleanup_verified=True)
    status, report = capture.take_report(outcome)
    assert status == "available" and report is not None
    assert capture.take_report(outcome) == ("invalid", None)


@pytest.mark.parametrize("abort", [False, True])
def test_pytest_capture_releases_no_report_on_unknown_or_abort(abort: bool) -> None:
    capture = _collector(_payload())
    capture.seal(abort=abort)
    outcome = DockerJobOutcome(exit_code=1, cleanup_verified=True, outcome_unknown=not abort)
    assert capture.take_report(outcome) == ("invalid", None)


@pytest.mark.parametrize("sensitive_stderr", [False, True])
def test_pytest_capture_withholds_sensitive_decoded_text(sensitive_stderr: bool) -> None:
    payload = _payload()
    log = b"pytest summary\n"
    if sensitive_stderr:
        log = b"token: synthetic\n"
    else:
        payload = payload.replace(b"assert 0", b"\\u0074oken: synthetic")
    capture = _collector(payload, log)
    capture.seal()
    assert capture.take_report(DockerJobOutcome(exit_code=1, cleanup_verified=True)) == (
        "withheld",
        None,
    )
