"""Storage facts never imply complete application readiness or expose settings bodies."""

import pytest
from pydantic import ValidationError

from jeff.application.storage import (
    DEFAULT_SETTINGS,
    HistorySettings,
    RuntimeSettings,
    StorageReadiness,
)


def test_settings_defaults_are_strict_frozen_and_reject_unknown_fields() -> None:
    assert DEFAULT_SETTINGS.schema_version == 1
    assert DEFAULT_SETTINGS.history.retention_days == 30
    with pytest.raises(ValidationError):
        DEFAULT_SETTINGS.history.retention_days = 7
    for value in (True, "30", 0, -1):
        with pytest.raises(ValidationError):
            HistorySettings.model_validate({"retention_days": value})
    with pytest.raises(ValidationError) as caught:
        RuntimeSettings.model_validate(
            {"schema_version": 1, "history": {"retention_days": 30}, "extra": "SYNTHETIC_PRIVATE"}
        )
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)


def test_ready_requires_real_schema_and_settings_and_failed_requires_safe_code() -> None:
    assert StorageReadiness().state == "not_checked"
    with pytest.raises(ValidationError):
        StorageReadiness(state="ready")
    with pytest.raises(ValidationError):
        StorageReadiness(state="failed")
    ready = StorageReadiness(
        state="ready", schema_version=2, retention_days=30, settings_source="default_file"
    )
    assert ready.schema_version == 2
    with pytest.raises(ValidationError):
        StorageReadiness(
            state="ready", schema_version=True, retention_days=30, settings_source="default_file"
        )
