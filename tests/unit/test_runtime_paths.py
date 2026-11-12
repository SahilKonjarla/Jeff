"""Pure explicit-input location selection; no real home or environment defaults."""

from pathlib import Path

import pytest

from jeff.application.projects import Failure
from jeff.application.runtime import RuntimeLayout, RuntimePathInputs
from jeff.infrastructure.runtime_paths import resolve_layout


def inputs(**changes: object) -> RuntimePathInputs:
    values: dict[str, object] = {
        "platform": "darwin",
        "home": Path("/Users/example"),
        "temp_dir": Path("/private/tmp"),
        "uid": 1234,
    }
    values.update(changes)
    return RuntimePathInputs(**values)  # type: ignore[arg-type]


def test_mac_layout_uses_explicit_host_inputs_and_fixed_filenames() -> None:
    result = resolve_layout(inputs())
    assert isinstance(result, RuntimeLayout)
    assert result.data_dir == Path("/Users/example/Library/Application Support/Jeff")
    assert result.runtime_dir == Path("/private/tmp/jeff-1234")
    assert result.database.name == "state.sqlite3"
    assert result.settings.name == "settings.yaml"
    assert result.credential.name == "ipc.key"
    assert result.socket == Path("/private/tmp/jeff-1234/daemon.sock")
    assert result.lock.name == "daemon.lock"


def test_overrides_are_independent_and_do_not_follow_cwd_or_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JEFF_DATA_DIR", "SYNTHETIC_PRIVATE_NOT_AN_INPUT")
    result = resolve_layout(inputs(runtime_dir="/private/tmp/test-run"))
    assert isinstance(result, RuntimeLayout)
    assert result.data_dir == Path("/Users/example/Library/Application Support/Jeff")
    assert result.runtime_dir == Path("/private/tmp/test-run")
    other = resolve_layout(inputs(data_dir="/private/tmp/test-data"))
    assert isinstance(other, RuntimeLayout)
    assert other.data_dir == Path("/private/tmp/test-data")
    assert other.runtime_dir == Path("/private/tmp/jeff-1234")


@pytest.mark.parametrize("field", ["data_dir", "runtime_dir"])
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "relative",
        "~/hidden",
        "/private/../hidden",
        "/nul\0path",
        "//ambiguous/path",
        "/bad\ud800",
    ],
)
def test_invalid_override_returns_fixed_error_without_echo(field: str, bad: str) -> None:
    result = resolve_layout(inputs(**{field: bad}))
    assert isinstance(result, Failure)
    assert result.error.code == "invalid_runtime_path"
    if bad:
        assert bad not in result.model_dump_json()
    assert result.error.message


def test_socket_limit_uses_bytes_including_filename_not_character_count() -> None:
    accepted = resolve_layout(inputs(runtime_dir="/" + "x" * 90))
    assert isinstance(accepted, RuntimeLayout)
    rejected = resolve_layout(inputs(runtime_dir="/" + "x" * 91))
    assert isinstance(rejected, Failure)
    assert rejected.error.code == "runtime_path_too_long"
    multibyte = resolve_layout(inputs(runtime_dir="/" + "é" * 46))
    assert isinstance(multibyte, Failure)
    assert multibyte.error.code == "runtime_path_too_long"


def test_documented_mac_base_aliases_are_normalized_lexically_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Resolver must not inspect filesystem or resolve links")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", forbidden)
        patch.setattr(Path, "stat", forbidden)
        patch.setattr(Path, "lstat", forbidden)
        result = resolve_layout(inputs(temp_dir=Path("/var/folders/sample"), data_dir="/tmp/data"))
    assert isinstance(result, RuntimeLayout)
    assert result.data_dir == Path("/private/tmp/data")
    assert result.runtime_dir == Path("/private/var/folders/sample/jeff-1234")


@pytest.mark.parametrize("uid", [-1, True, "1234"])
def test_invalid_owner_is_not_coerced(uid: object) -> None:
    result = resolve_layout(inputs(uid=uid))
    assert isinstance(result, Failure)
    assert result.error.code == "invalid_runtime_path"


@pytest.mark.parametrize("path", ["/" + "aa/" * 400 + "data", "/" + "x" * 256])
def test_data_paths_are_bounded_before_filesystem_walks(path: str) -> None:
    result = resolve_layout(inputs(data_dir=path))
    assert isinstance(result, Failure)
    assert result.error.code == "runtime_path_too_long"


def test_unverified_platform_is_explicit_not_claimed_supported() -> None:
    result = resolve_layout(inputs(platform="linux"))
    assert isinstance(result, Failure)
    assert result.error.code == "unsupported_runtime_platform"
