"""Original function-scoped explicit Darwin layout over short private temporary roots."""

import os

import pytest

from jeff.application.runtime import RuntimeLayout, RuntimePathInputs
from jeff.infrastructure.runtime_paths import resolve_layout


@pytest.fixture
def layout(tmp_path_factory: pytest.TempPathFactory) -> RuntimeLayout:
    arena = tmp_path_factory.mktemp("rt").resolve()
    result = resolve_layout(
        RuntimePathInputs(
            platform="darwin",
            home=arena,
            temp_dir=arena,
            uid=os.geteuid(),
            data_dir=str(arena / "d"),
            runtime_dir=str(arena / "r"),
        )
    )
    assert isinstance(result, RuntimeLayout)
    return result
