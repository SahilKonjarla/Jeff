"""Bounded strict private YAML; no rewrite/repair of an existing settings file."""

import os
import stat
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from jeff.application.runtime import ControlFault
from jeff.application.storage import SETTINGS_LIMIT, RuntimeSettings, StorageFault
from jeff.infrastructure.runtime_state import Ownership

_DEFAULT_BYTES = b"schema_version: 1\nhistory:\n  retention_days: 30\n"
_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class _UniqueLoader(yaml.SafeLoader):  # type: ignore[misc]
    def construct_mapping(self, node: Any, deep: bool = False) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise ValueError("Invalid mapping")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def parse_settings(payload: bytes) -> RuntimeSettings:
    try:
        if len(payload) > SETTINGS_LIMIT:
            raise ValueError("Size")
        text = payload.decode("utf-8", errors="strict")
        depth = 0
        for count, event in enumerate(yaml.parse(text), start=1):
            if (
                count > 4096
                or isinstance(event, yaml.events.AliasEvent)
                or getattr(event, "anchor", None) is not None
                or getattr(event, "tag", None) is not None
            ):
                raise ValueError("Structure")
            if isinstance(event, yaml.events.CollectionStartEvent):
                depth += 1
                if depth > 20:
                    raise ValueError("Depth")
            elif isinstance(event, yaml.events.CollectionEndEvent):
                depth -= 1
        return RuntimeSettings.model_validate(yaml.load(text, Loader=_UniqueLoader))
    except (ValueError, TypeError, RecursionError, yaml.YAMLError, ValidationError):
        raise StorageFault("settings_invalid") from None


def load_or_create_settings(
    owner: Ownership,
) -> tuple[RuntimeSettings, Literal["default_file", "existing_file"]]:
    try:
        owner.check()
        source: Literal["default_file", "existing_file"] = "existing_file"
        with owner.store.directory(owner.store.layout.data_dir, create=True) as root:
            try:
                fd = os.open(
                    "settings.yaml",
                    os.O_WRONLY | _FLAGS | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root,
                )
            except FileExistsError:
                pass
            else:
                try:
                    offset = 0
                    while offset < len(_DEFAULT_BYTES):
                        written = os.write(fd, _DEFAULT_BYTES[offset:])
                        if written <= 0:
                            raise StorageFault("settings_invalid")
                        offset += written
                    os.fsync(fd)
                    source = "default_file"
                finally:
                    os.close(fd)
            fd = os.open("settings.yaml", os.O_RDONLY | _FLAGS, dir_fd=root)
            try:
                before = os.fstat(fd)
                if not (
                    stat.S_ISREG(before.st_mode)
                    and before.st_uid == owner.store.layout.uid
                    and stat.S_IMODE(before.st_mode) == 0o600
                    and before.st_nlink == 1
                    and before.st_size <= SETTINGS_LIMIT
                ):
                    raise StorageFault("settings_invalid")
                chunks = bytearray()
                while len(chunks) <= SETTINGS_LIMIT:
                    part = os.read(fd, SETTINGS_LIMIT + 1 - len(chunks))
                    if not part:
                        break
                    chunks.extend(part)
                after = os.stat("settings.yaml", dir_fd=root, follow_symlinks=False)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_uid,
                    before.st_mode,
                    before.st_nlink,
                    before.st_size,
                    before.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_uid,
                    after.st_mode,
                    after.st_nlink,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise StorageFault("settings_invalid")
                settings = parse_settings(bytes(chunks))
            finally:
                os.close(fd)
        owner.check()
        return settings, source
    except (OSError, ControlFault):
        raise StorageFault("settings_invalid") from None
