"""Bounded metadata query grammar, templates and explicit reader contract."""

from typing import Literal, Protocol

from jeff.application.execution import DockerCLIIdentity

Query = Literal["client.version", "engine.info", "image.inspect"]


_LIMIT = 65536


_ENGINE_FORMAT = (
    '{"ID":{{json .ID}},"OSType":{{json .OSType}},"Architecture":{{json .Architecture}},'
    '"OperatingSystem":{{json .OperatingSystem}},"ServerVersion":{{json .ServerVersion}},'
    '"SecurityOptions":{{json .SecurityOptions}},"MemoryLimit":{{json .MemoryLimit}},'
    '"PidsLimit":{{json .PidsLimit}},"CPUCfsPeriod":{{json .CPUCfsPeriod}},'
    '"CPUCfsQuota":{{json .CPUCfsQuota}}}'
)


_IMAGE_FORMAT = (
    '{"ID":{{json .Id}},"OS":{{json .Os}},"Architecture":{{json .Architecture}},'
    '"Variant":{{json .Variant}},"Config":{{json .Config}}}'
)


class _Reader(Protocol):
    identity: DockerCLIIdentity

    def check(self) -> None: ...

    def read(
        self, query: Query, *, endpoint: str, image_id: str | None, deadline: float
    ) -> bytes: ...
