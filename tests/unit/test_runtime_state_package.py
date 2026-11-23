"""Synthetic ownership proof contracts and native identity ABI across file extraction."""

import ctypes
import inspect
import json
import os
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

from pydantic import BaseModel

from jeff.application import runtime as runtime_contracts
from jeff.application.runtime import RuntimeLayout
from jeff.infrastructure import runtime_state as native

SCHEMA_DIGEST = "298f68b4231f2065d44b0c054e0be9ad34ace40089a9853713d2a24105c54ae7"
PROOF_DIGEST = "d6b9ec72e9e7953dca3cbdaeefb525364c2f40c7fa9020bc932ff06be68e0405"
ABI_DIGEST = "b224731b64c493197c3ae7e85f02687e3378bd7ab500011f7dd101a6c8131269"
SIGNATURE_DIGEST = "abf64d06a1d72dc4b01b47dc180bb847d90ac3cea35b0ef3a4d2a13dc7d92ac7"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _schemas() -> dict[str, object]:
    return {
        name: value.model_json_schema()
        for name, value in vars(native).items()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__.startswith("jeff.infrastructure.runtime_state")
    }


def _receipt() -> native._EndpointReceipt:
    identity = native._FileIdentity(device=1, inode=2, birth_ns=3)
    return native._EndpointReceipt(
        version=1,
        instance_id=str(UUID(int=1)),
        nonce="1" * 32,
        bindings=native._EndpointBindings(
            runtime=identity,
            data=identity,
            lock=identity,
            key=identity,
            socket=identity,
            receipt=identity,
        ),
        mac="0" * 64,
    )


def _permit() -> native._RecoveryPermit:
    identity = native._FileIdentity(device=1, inode=2, birth_ns=3)
    return native._RecoveryPermit(
        version=1,
        schema_version=10,
        nonce="2" * 32,
        page_size=4096,
        original_pages=16,
        bindings=native._RecoveryBindings(
            runtime=identity,
            data=identity,
            lock=identity,
            key=identity,
            database=identity,
            journal=identity,
            permit=identity,
        ),
        mac="0" * 64,
    )


def _proofs() -> dict[str, object]:
    # Fixed synthetic DTOs/key are test vectors, never installation proofs or credentials.
    receipt, permit = _receipt(), _permit()
    return {
        "receipt": receipt.model_dump_json(),
        "permit": permit.model_dump_json(),
        "receipt_tag": native._receipt_tag(receipt, bytes(32)),
        "recovery_tag": native._recovery_tag(permit, bytes(32)),
        "database_cap": native.RECOVERY_DATABASE_BYTES,
        "journal_cap": native.RECOVERY_JOURNAL_BYTES,
        "file_flags": native._FILE_FLAGS,
    }


def _abi() -> dict[str, object]:
    return {
        "pointer": ctypes.sizeof(ctypes.c_void_p),
        "timespec": ctypes.sizeof(native._Timespec),
        "stat": ctypes.sizeof(native._DarwinStat),
        "fields": {
            field[0]: [getattr(native._DarwinStat, field[0]).offset, ctypes.sizeof(field[1])]
            for field in native._DarwinStat._fields_
        },
    }


def _signatures() -> dict[str, str]:
    result = {}
    aliases = [
        name
        for name, value in vars(native).items()
        if inspect.isclass(value)
        and value.__module__.startswith("jeff.infrastructure.runtime_state")
    ]
    for name in ("DiagnosticLease", "DiagnosticObservation", "Ownership", "RuntimeStateStore"):
        for method_name, value in vars(getattr(native, name)).items():
            if isinstance(value, property):
                value = value.fget
            if isinstance(value, (staticmethod, classmethod)):
                value = value.__func__
            if not inspect.isfunction(value):
                continue
            signature = str(inspect.signature(value)).replace(
                "_state.RuntimeStateStore", "RuntimeStateStore"
            )
            for alias in aliases:
                annotation = getattr(native, alias)
                signature = signature.replace(
                    f"{annotation.__module__}.{annotation.__qualname__}",
                    f"jeff.infrastructure.runtime_state.{alias}",
                )
            for alias, annotation in vars(runtime_contracts).items():
                if inspect.isclass(annotation) and annotation.__module__.startswith(
                    "jeff.application.runtime"
                ):
                    signature = signature.replace(
                        f"{annotation.__module__}.{annotation.__qualname__}",
                        f"jeff.application.runtime.{alias}",
                    )
            result[f"{name}.{method_name}"] = signature
            get_type_hints(value)
    return result


def test_proof_model_schemas_remain_identical() -> None:
    assert _digest(_schemas()) == SCHEMA_DIGEST


def test_proof_serialization_hmac_domains_and_caps_remain_identical() -> None:
    assert _digest(_proofs()) == PROOF_DIGEST


def test_native_abi_structure_layout_remains_identical() -> None:
    assert _digest(_abi()) == ABI_DIGEST


def test_public_class_interfaces_and_resolved_store_annotations_remain_identical() -> None:
    assert _digest(_signatures()) == SIGNATURE_DIGEST
    for cls in (native.DiagnosticLease, native.DiagnosticObservation, native.Ownership):
        assert get_type_hints(cls.__init__)["store"] is native.RuntimeStateStore


def test_closing_owner_copy_does_not_unlock_duplicated_live_description(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    store = native.RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    duplicate = os.dup(owner.fd)
    try:
        owner.close()
        probe = store.acquire(create=False)
        try:
            assert probe is None
        finally:
            if probe is not None:
                probe.close()
    finally:
        owner.close()
        os.close(duplicate)
    unlocked = store.acquire(create=False)
    assert unlocked is not None
    unlocked.close()
    assert layout.lock.exists()


def test_package_files_stay_within_size_limit(code_line_count: Callable[[str], int]) -> None:
    for path in Path(native.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name
