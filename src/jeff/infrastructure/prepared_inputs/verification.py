"""Verify sealed bytes and exact bindings without recopying live source."""

import json
import os
from contextlib import ExitStack
from typing import TYPE_CHECKING
from uuid import UUID

from jeff.application.execution import ExecutionManifest, PreparedInputBinding, PreparedInputFault
from jeff.application.runtime import ControlFault

from .filesystem import _deadline, _identity, _names
from .metadata import _SEAL_BYTES, _Seal, _unique_object

if TYPE_CHECKING:
    from .store import FilePreparedInputStore


def verify(
    self: "FilePreparedInputStore", manifest: ExecutionManifest, *, deadline: float
) -> PreparedInputBinding:
    try:
        if not isinstance(manifest.inputs.snapshot_id, UUID):
            raise PreparedInputFault("input_invalid")
        self._literal_paths(tuple(item.path for item in manifest.inputs.files))
        with ExitStack() as retained:
            with self._base(
                create=False, exclusive=False, deadline=deadline, retained=retained
            ) as jobs:
                snapshot = self._directory(jobs, str(manifest.inputs.snapshot_id), 0o700, retained)
                _names(snapshot, {"input", "prepared.json"}, deadline)
                with self._file(
                    snapshot, "prepared.json", deadline, mode=0o600, limit=_SEAL_BYTES
                ) as fd:
                    payload = bytearray()
                    while True:
                        _deadline(deadline)
                        block = os.read(fd, min(65536, _SEAL_BYTES + 1 - len(payload)))
                        if not block:
                            break
                        payload.extend(block)
                        if len(payload) > _SEAL_BYTES:
                            raise PreparedInputFault("input_limit")
                seal = _Seal.model_validate(json.loads(payload, object_pairs_hook=_unique_object))
                if (
                    seal.project_id != manifest.profile.project_id
                    or seal.canonical_root != manifest.profile.canonical_root
                    or seal.inventory != manifest.inputs
                    or seal.inventory_digest != manifest.inputs.digest
                ):
                    raise PreparedInputFault("input_scope_mismatch")
                view = self._directory(snapshot, "input", 0o555, retained)
                if _identity(os.fstat(view)) != seal.input_identity:
                    raise PreparedInputFault("input_changed")
                self._verify_tree(view, manifest.inputs, deadline)
                return PreparedInputBinding(
                    snapshot_id=manifest.inputs.snapshot_id,
                    inventory_digest=manifest.inputs.digest,
                    path=self.data / "jobs" / str(manifest.inputs.snapshot_id) / "input",
                )
    except PreparedInputFault:
        raise
    except FileNotFoundError:
        raise PreparedInputFault("input_unsealed") from None
    except (OSError, ControlFault, ValueError):
        raise PreparedInputFault("input_unconfirmed") from None


def check(
    self: "FilePreparedInputStore",
    binding: PreparedInputBinding,
    manifest: ExecutionManifest,
    *,
    deadline: float,
) -> None:
    if not isinstance(manifest.inputs.snapshot_id, UUID):
        raise PreparedInputFault("input_invalid")
    try:
        expected = PreparedInputBinding(
            snapshot_id=manifest.inputs.snapshot_id,
            inventory_digest=manifest.inputs.digest,
            path=self.data / "jobs" / str(manifest.inputs.snapshot_id) / "input",
        )
    except ValueError:
        raise PreparedInputFault("input_invalid") from None
    if binding != expected:
        raise PreparedInputFault("input_scope_mismatch")
    if self.verify(manifest, deadline=deadline) != binding:
        raise PreparedInputFault("input_changed")
