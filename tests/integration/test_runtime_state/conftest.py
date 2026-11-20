"""Function-scoped selected roots and signed endpoint ownership; preserve yield cleanup."""

import os
import socket
from collections.abc import Iterator
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore


@pytest.fixture
def layout(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RuntimeLayout]:
    root = tmp_path_factory.mktemp("st").resolve()
    result = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    yield result
    result.credential.unlink(missing_ok=True)


@pytest.fixture
def recorded(layout: RuntimeLayout) -> Iterator[tuple[Ownership, bytes]]:
    store = RuntimeStateStore(layout)
    owner = store.acquire(create=True)
    assert owner is not None
    with owner, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        token = store.credential(owner)
        endpoint.bind(str(layout.socket))
        layout.socket.chmod(0o600)
        owner.publish_endpoint(uuid4(), token)
        endpoint.close()
        yield owner, token
