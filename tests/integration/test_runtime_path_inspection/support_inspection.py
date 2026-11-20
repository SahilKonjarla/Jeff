"""Canonical synthetic private entries and metadata-only observation helper."""

from jeff.application.runtime import RuntimeLayout, RuntimePathInspection
from jeff.infrastructure.runtime_paths import inspect_layout


def private_files(layout: RuntimeLayout) -> None:
    layout.data_dir.mkdir(mode=0o700)
    layout.runtime_dir.mkdir(mode=0o700)
    for path in (layout.database, layout.settings, layout.credential, layout.lock):
        path.write_bytes(b"SYNTHETIC_FIXTURE_CONTENT")
        path.chmod(0o600)


def observations(layout: RuntimeLayout) -> dict[str, tuple[str, str | None]]:
    report = inspect_layout(layout)
    assert isinstance(report, RuntimePathInspection)
    return {entry.role: (entry.state, entry.issue) for entry in report.entries}
