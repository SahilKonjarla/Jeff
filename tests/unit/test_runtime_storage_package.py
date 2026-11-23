"""Interface metadata, SQL catalog and same-owner override seams across extraction."""

import inspect
import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import pytest

from jeff.application.execution import DockerExecutionResult
from jeff.application.storage import Receipt, ReceiptScope
from jeff.infrastructure import runtime_storage as native

# Listing read methods and the finite actor operation/scope are deliberate additions.
INTERFACE_DIGEST = "b10c1c15ac522f73cdb4c4c2b2c4448a66dfe85581b5303dc20caf9f409e4687"
CATALOG_DIGEST = "866b0e2f909b5e4920cd17ed29d3fbe963eb1f790bc03c35beaf7512a6b7f9b5"
WORK_DIGEST = "ea6d63112a22d7b73ac21b4b325a13cf1ff7a98de3a5692563e56ff9df356b8f"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _canonical(value: str) -> str:
    for name in ("SQLiteStorage", "_Work"):
        cls = getattr(native, name)
        value = value.replace(f"{cls.__module__}.{cls.__qualname__}", f"{native.__name__}.{name}")
    return value


def _interfaces() -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in vars(native.SQLiteStorage).items():
        kind = "method"
        if isinstance(value, property):
            value, kind = value.fget, "property"
        if isinstance(value, staticmethod):
            value, kind = value.__func__, "static"
        if not inspect.isfunction(value):
            continue
        result[name] = {
            "signature": _canonical(str(inspect.signature(value))),
            "hints": {key: _canonical(str(hint)) for key, hint in get_type_hints(value).items()},
            "kind": kind,
            "doc": inspect.getdoc(value),
        }
    result["inspect_stopped_storage"] = str(inspect.signature(native.inspect_stopped_storage))
    get_type_hints(native.inspect_stopped_storage)
    return result


def _catalog() -> dict[str, object]:
    return {
        name: value
        for name, value in vars(native).items()
        if name.startswith("_")
        and name[1:].isupper()
        and isinstance(value, (str, int, tuple, dict))
    }


def _work() -> dict[str, object]:
    return {
        "signature": _canonical(str(inspect.signature(native._Work))),
        "hints": {key: _canonical(str(hint)) for key, hint in get_type_hints(native._Work).items()},
        "frozen": vars(native._Work)["__dataclass_params__"].frozen,
        "fields": list(native._Work.__dataclass_fields__),
    }


def test_store_signatures_properties_static_descriptors_and_docs_remain_identical() -> None:
    assert _digest(_interfaces()) == INTERFACE_DIGEST


def test_sql_catalog_sidecars_and_file_flags_remain_identical() -> None:
    assert _digest(_catalog()) == CATALOG_DIGEST


def test_work_envelope_interface_and_mutability_remain_identical() -> None:
    assert _digest(_work()) == WORK_DIGEST
    work = native._Work("lookup", None, 1.0, Future())
    work.deadline = 2.0
    assert work.deadline == 2.0


def test_static_scoped_receipt_keeps_replaceable_root_class_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = Receipt(
        request_id=UUID(int=1),
        owner_instance_id=UUID(int=2),
        operation="ask.branch",
        canonical_root=Path("/synthetic/project"),
        project_id=UUID(int=3),
        state="accepted",
        accepted_at_ms=0,
    )
    scope = ReceiptScope(
        request_id=expected.request_id,
        canonical_root=expected.canonical_root,
        project_id=expected.project_id,
    )
    seen: list[tuple[object, ...]] = []
    original = native.SQLiteStorage

    class Replaced(native.SQLiteStorage):
        @staticmethod
        def _receipt(row: tuple[object, ...]) -> Receipt:
            seen.append(row)
            return expected

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(native._RECEIPTS)
        connection.execute(
            "INSERT INTO request_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(expected.request_id),
                str(expected.owner_instance_id),
                expected.operation,
                str(expected.canonical_root),
                str(expected.project_id),
                "accepted",
                0,
                None,
                None,
            ),
        )
        with monkeypatch.context() as patch:
            patch.setattr(native, "SQLiteStorage", Replaced)
            assert original._scoped_receipt(connection, scope) == expected
    assert len(seen) == 1


def test_static_payload_retirement_keeps_replaceable_root_class_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = native.SQLiteStorage
    seen: list[sqlite3.Connection] = []

    class Replaced(native.SQLiteStorage):
        @staticmethod
        def _retained_payloads(
            connection: sqlite3.Connection,
        ) -> list[tuple[str, str, str, int, int]]:
            seen.append(connection)
            return []

    with closing(sqlite3.connect(":memory:")) as connection:
        with monkeypatch.context() as patch:
            patch.setattr(native, "SQLiteStorage", Replaced)
            assert original._retire_execution_payloads(connection, 0) == 0
        assert seen == [connection]


def test_overridden_work_dispatch_and_checks_keep_same_receiver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, ...]] = []
    sentinel = object()

    class Store(native.SQLiteStorage):
        def __init__(self) -> None:
            pass

        def _check(self, root: int, fd: int) -> None:
            seen.append((self, root, fd))

        def _receipt_work(
            self, connection: sqlite3.Connection, work: native._Work, root: int, fd: int
        ) -> object:
            seen.append((self, connection, work, root, fd))
            return sentinel

    store = Store()
    work = native._Work("inspect", None, 1.0, Future())
    with closing(sqlite3.connect(":memory:")) as connection:
        assert store._execute(connection, work, 11, 12) is sentinel
        assert seen == [(store, 11, 12), (store, connection, work, 11, 12)]
        received: list[object] = []

        def instance_hook(
            connection: sqlite3.Connection, work: native._Work, root: int, fd: int
        ) -> object:
            received.extend((connection, work, root, fd))
            return sentinel

        seen.clear()
        with monkeypatch.context() as patch:
            patch.setattr(store, "_receipt_work", instance_hook)
            assert store._execute(connection, work, 13, 14) is sentinel
        assert seen == [(store, 13, 14)]
        assert received == [connection, work, 13, 14]


def test_completion_result_static_signature_still_resolves() -> None:
    assert (
        get_type_hints(native.SQLiteStorage._result_stop_terminal)["result"]
        is DockerExecutionResult
    )


def test_package_files_stay_within_size_limit(code_line_count: Callable[[str], int]) -> None:
    for path in Path(native.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name
