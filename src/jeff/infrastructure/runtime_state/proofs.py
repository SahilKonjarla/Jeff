"""Frozen endpoint/storage proofs, canonical authentication and bounded entry checks."""

import hashlib
import hmac
import json
import os
import stat

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from jeff.application.runtime import ControlFault


class _ProofModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class _FileIdentity(_ProofModel):
    device: StrictInt = Field(ge=-(1 << 31), le=(1 << 32) - 1)
    inode: StrictInt = Field(gt=0, le=(1 << 64) - 1)
    birth_ns: StrictInt = Field(gt=0, le=(1 << 63) - 1)


class _EndpointBindings(_ProofModel):
    runtime: _FileIdentity
    data: _FileIdentity
    lock: _FileIdentity
    key: _FileIdentity
    socket: _FileIdentity
    receipt: _FileIdentity


class _EndpointReceipt(_ProofModel):
    version: StrictInt = Field(ge=1, le=1)
    instance_id: str = Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    bindings: _EndpointBindings
    mac: str = Field(pattern=r"^[0-9a-f]{64}$")


class _RecoveryBindings(_ProofModel):
    runtime: _FileIdentity
    data: _FileIdentity
    lock: _FileIdentity
    key: _FileIdentity
    database: _FileIdentity
    journal: _FileIdentity
    permit: _FileIdentity


class _RecoveryPermit(_ProofModel):
    version: StrictInt = Field(ge=1, le=1)
    schema_version: StrictInt = Field(ge=4, le=12)
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    page_size: StrictInt = Field(ge=512, le=65536)
    original_pages: StrictInt = Field(gt=0, le=(1 << 32) - 1)
    bindings: _RecoveryBindings
    mac: str = Field(pattern=r"^[0-9a-f]{64}$")


RECOVERY_DATABASE_BYTES = 256 * 1024 * 1024


RECOVERY_JOURNAL_BYTES = 16 * 1024 * 1024


def _recovery_tag(permit: _RecoveryPermit, token: bytes) -> str:
    payload = json.dumps(
        permit.model_dump(exclude={"mac"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(token, b"jeff.storage.recovery.v1\0" + payload, hashlib.sha256).hexdigest()


def _recovery_entry(root: int, uid: int) -> os.stat_result | None:
    try:
        info = os.stat("recovery.json", dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == uid
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and 0 < info.st_size <= 2048
    ):
        raise ControlFault("unsafe_runtime_path")
    return info


def _receipt_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate receipt field")
        result[key] = value
    return result


def _receipt_tag(receipt: _EndpointReceipt, token: bytes) -> str:
    payload = json.dumps(
        receipt.model_dump(exclude={"mac"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(token, b"jeff.endpoint.receipt.v1\0" + payload, hashlib.sha256).hexdigest()


def _receipt_entry(root: int, uid: int) -> os.stat_result | None:
    try:
        info = os.stat("endpoint.json", dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == uid
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and 0 < info.st_size <= 2048
    ):
        raise ControlFault("unsafe_runtime_path")
    return info
