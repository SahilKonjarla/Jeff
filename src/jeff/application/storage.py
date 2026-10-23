"""Private settings/storage facts and validated registry use cases, not project RPC."""

from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from jeff.application.projects import ErrorData, Failure, ProjectService
from jeff.domain.projects import Project

SETTINGS_SCHEMA = 1
DATABASE_SCHEMA = 1
DATABASE_APPLICATION_ID = int.from_bytes(b"JEFF", "big")
STORAGE_PENDING_LIMIT = 16
SETTINGS_LIMIT = 65536
StorageIssueCode = Literal[
    "settings_invalid",
    "storage_unsafe",
    "storage_unrecognized",
    "storage_schema_unsupported",
    "storage_recovery_required",
    "storage_failed",
    "storage_busy",
    "storage_closing",
    "storage_outcome_unconfirmed",
    "registry_conflict",
]
_MESSAGES: dict[StorageIssueCode, str] = {
    "settings_invalid": "Private settings are invalid or unsafe; existing bytes were not replaced.",
    "storage_unsafe": "Private storage paths or observed ownership are unsafe.",
    "storage_unrecognized": "The database is not recognized as Jeff storage; it was not reset.",
    "storage_schema_unsupported": (
        "Database format/schema/ledger unsupported; no migration was applied."
    ),
    "storage_recovery_required": "Existing sidecars require explicit recovery; none were removed.",
    "storage_failed": "Storage failed; no automatic reset was attempted.",
    "storage_busy": "Storage is busy or its bounded queue is full; work was not replayed.",
    "storage_closing": "Storage is not accepting work.",
    "storage_outcome_unconfirmed": (
        "Storage operation/closure outcome unconfirmed; do not replay automatically."
    ),
    "registry_conflict": "The indexed project identity and root conflict; no rebind was performed.",
}


class StorageFault(Exception):
    def __init__(self, code: StorageIssueCode) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])


def storage_failure(code: StorageIssueCode) -> Failure:
    return Failure(error=ErrorData(code=code, message=_MESSAGES[code]))


class _StorageModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class HistorySettings(_StorageModel):
    retention_days: StrictInt = Field(gt=0)


class RuntimeSettings(_StorageModel):
    schema_version: StrictInt = Field(ge=SETTINGS_SCHEMA, le=SETTINGS_SCHEMA)
    history: HistorySettings


DEFAULT_SETTINGS = RuntimeSettings(schema_version=1, history=HistorySettings(retention_days=30))


class StorageReadiness(_StorageModel):
    state: Literal["not_checked", "initializing", "ready", "failed", "closing"] = "not_checked"
    schema_version: StrictInt | None = Field(default=None, ge=DATABASE_SCHEMA, le=DATABASE_SCHEMA)
    retention_days: StrictInt | None = Field(default=None, gt=0)
    settings_source: Literal["default_file", "existing_file"] | None = None
    error: StorageIssueCode | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        has_facts = (
            self.schema_version is not None
            and self.retention_days is not None
            and self.settings_source is not None
        )
        any_facts = any(
            value is not None
            for value in (self.schema_version, self.retention_days, self.settings_source)
        )
        if (self.state == "ready" and not has_facts) or (self.state != "ready" and any_facts):
            raise ValueError("Only validated ready storage has complete schema/settings facts.")
        if (self.state == "failed") != (self.error is not None):
            raise ValueError("Only failed storage requires a bounded error code.")
        return self


class ProjectRegistry(Protocol):
    def index(self, project: Project) -> tuple[Project, bool]:
        """Return immutable entry + created; identical claims are no-ops, conflicts fail."""
        ...

    def lookup(self, project: Project) -> Project | None:
        """Return immutable entry or no-index; never index/bless an unvalidated project."""
        ...


class RegistryData(_StorageModel):
    project_id: UUID
    root: Path
    indexed: StrictBool
    created: StrictBool = False


class RegistrySuccess(_StorageModel):
    ok: Literal[True] = True
    data: RegistryData
    error: None = None


class RegistryService:
    """Explicit callers outside the control loop; YAML remains identity authority."""

    def __init__(self, projects: ProjectService, registry: ProjectRegistry) -> None:
        self.projects = projects
        self.registry = registry

    def execute(
        self, operation: Literal["index", "lookup"], directory: Path
    ) -> RegistrySuccess | Failure:
        bound = self.projects.execute("config", directory)
        if isinstance(bound, Failure):
            return bound
        project = Project(UUID(bound.data.project_id), Path(bound.data.root))
        try:
            entry: Project | None
            if operation == "index":
                entry, created = self.registry.index(project)
            else:
                entry, created = self.registry.lookup(project), False
            if entry is not None and entry != project:
                return storage_failure("registry_conflict")
            indexed = entry is not None
            return RegistrySuccess(
                data=RegistryData(
                    project_id=project.project_id,
                    root=project.root,
                    indexed=indexed,
                    created=created,
                )
            )
        except StorageFault as error:
            return storage_failure(error.code)
