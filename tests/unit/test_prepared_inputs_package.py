"""Characterize prepared snapshot metadata, limits and store interfaces."""

import inspect
import json
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import pytest

from jeff.application.execution import PreparedFile, PreparedInputs
from jeff.infrastructure import prepared_inputs as native
from jeff.infrastructure.runtime_state import Ownership

SCHEMA_DIGEST = "a41926d8ea1954841c45519e8418540ef0796fd5bd610f2b2428edd8243cce55"
METADATA_DIGEST = "0a24a807c9296ee714150a5bc201d60fd8fcb6426147cc3bc57b8143b4a38fd5"
LIMIT_DIGEST = "944a50cbd867f293e9e298257eefdda0eb5e0f311b3270a12a47d466bd8b837b"
SIGNATURE_DIGEST = "bf86f9a1884f3a2f677ee296aa291d354c9772a0c08e70b0a4466bf1752e52be"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _schema() -> dict[str, object]:
    return native._Seal.model_json_schema()


def _seal() -> native._Seal:
    inventory = PreparedInputs(
        snapshot_id=UUID(int=2),
        files=(PreparedFile(path="src/code.py", size=7, sha256="1" * 64),),
    )
    return native._Seal(
        project_id=UUID(int=1),
        canonical_root=Path("/synthetic/project"),
        inventory=inventory,
        inventory_digest=inventory.digest,
        input_identity=(1, 2, 3, 4),  # Synthetic metadata, never an actual inode/owner proof.
    )


def _metadata() -> dict[str, str]:
    seal = _seal()
    return {"seal": seal.model_dump_json(), "inventory_digest": seal.inventory.digest}


def _limits() -> dict[str, int]:
    return {
        "flags": native._FLAGS,
        "directory": native._DIR,
        "slots": native._SLOTS,
        "seal_bytes": native._SEAL_BYTES,
    }


def _signatures() -> dict[str, str]:
    result = {}
    for name, value in vars(native.FilePreparedInputStore).items():
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if inspect.isfunction(value):
            result[name] = str(inspect.signature(value)).replace(
                f"{Ownership.__module__}.{Ownership.__qualname__}",
                "jeff.infrastructure.runtime_state.Ownership",
            )
            get_type_hints(value)
    return result


def test_seal_schema_preserves_original_contract() -> None:
    assert _digest(_schema()) == SCHEMA_DIGEST


def test_seal_serialization_and_inventory_digest_stay_identical() -> None:
    seal = _seal()
    assert _digest(_metadata()) == METADATA_DIGEST
    assert native._Seal.model_validate_json(seal.model_dump_json()) == seal


def test_open_flags_and_capacity_limits_stay_identical() -> None:
    assert _digest(_limits()) == LIMIT_DIGEST


def test_store_signatures_and_annotations_stay_identical() -> None:
    assert _digest(_signatures()) == SIGNATURE_DIGEST


def test_duplicate_seal_keys_remain_rejected() -> None:
    with pytest.raises(ValueError):
        native._unique_object([("version", 1), ("version", 1)])


def test_package_files_stay_within_size_limit() -> None:
    for path in Path(native.__file__).parent.glob("*.py"):
        assert len(path.read_text().splitlines()) <= 400, path.name
