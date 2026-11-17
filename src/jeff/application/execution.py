"""Legacy reviews, distinct fresh public intent and private authority ports; no Docker I/O."""

import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import Future
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self, runtime_checkable
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from jeff.application.projects import ErrorData, Failure, ProjectService
from jeff.domain.execution import (
    ARGV_BYTES,
    ARGV_COUNT,
    PREPARED_FILE_BYTES,
    PREPARED_FILE_LIMIT,
    PREPARED_TOTAL_BYTES,
    AuthorityState,
    ReviewState,
    fingerprint,
    public_text,
)
from jeff.domain.projects import Project
from jeff.domain.repository import protected_path


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


def absolute_path(value: Path) -> Path:
    text = str(value)
    if not value.is_absolute() or ".." in value.parts or text.startswith("//"):
        raise ValueError("A canonical absolute path is required.")
    public_text(text, 1023)
    return value


class ExecutionProfile(_Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    project_id: UUID
    canonical_root: Path
    environment: Literal["development"] = "development"
    engine_id: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=8, max_length=1023)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/arm64", "linux/amd64"]
    toolchain: str = Field(min_length=1, max_length=128)
    executable: str = Field(min_length=1, max_length=255)
    container_cwd: Literal["/project"] = "/project"
    project_view: Literal["sanitized_read_only"] = "sanitized_read_only"
    scratch_target: Literal["/scratch"] = "/scratch"
    network: Literal["none"] = "none"
    user: Literal["65532:65532"] = "65532:65532"
    root_filesystem: Literal["read_only"] = "read_only"
    privileges: Literal["drop_all_no_new_privileges"] = "drop_all_no_new_privileges"
    log_driver: Literal["none"] = "none"
    cpu_millis: StrictInt = Field(default=1000, ge=1, le=1000)
    memory_bytes: StrictInt = Field(default=268435456, ge=16777216, le=268435456)
    pids: StrictInt = Field(default=64, ge=2, le=64)
    scratch_bytes: StrictInt = Field(default=67108864, ge=4096, le=67108864)
    timeout_seconds: StrictInt = Field(default=300, ge=1, le=3600)
    output_bytes: StrictInt = Field(default=1048576, ge=1, le=1048576)
    public_environment: tuple[tuple[str, str], ...] = ()

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @field_validator("engine_id", "toolchain", "executable", "endpoint")
    @classmethod
    def public(cls, value: str) -> str:
        return public_text(value, 1023)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.endpoint.startswith("unix:///"):
            raise ValueError("Only an explicitly declared local Unix endpoint is supported.")
        absolute_path(Path(self.endpoint[7:]))
        executable = PurePosixPath(self.executable)
        if (
            not executable.is_absolute()
            or ".." in executable.parts
            or self.executable.startswith("//")
        ):
            raise ValueError("The declared executable must be an absolute container path.")
        allowed = {
            "LANG": {"C", "C.UTF-8", "en_US.UTF-8"},
            "LC_ALL": {"C", "C.UTF-8"},
            "PYTHONDONTWRITEBYTECODE": {"1"},
        }
        keys = [key for key, _ in self.public_environment]
        if len(keys) != len(set(keys)) or any(
            key not in allowed or value not in allowed[key]
            for key, value in self.public_environment
        ):
            raise ValueError("Only fixed public environment values are supported.")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class ExecutionSettings(_Model):
    profiles: tuple[ExecutionProfile, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique(self) -> Self:
        identities = [(p.project_id, p.name) for p in self.profiles]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate project/profile binding.")
        return self


class ActionManifest(_Model):
    profile: ExecutionProfile
    argv: tuple[str, ...] = Field(min_length=1, max_length=ARGV_COUNT)
    authority_kind: Literal["review_only_v1"] = "review_only_v1"
    target_verification: Literal["declared_only"] = "declared_only"
    execution_available: Literal[False] = False

    @field_validator("argv")
    @classmethod
    def public_arguments(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            public_text(value)
        if sum(len(value.encode("utf-8")) for value in values) > ARGV_BYTES:
            raise ValueError("Argument budget exceeded.")
        return values

    @model_validator(mode="after")
    def exact(self) -> Self:
        if self.argv[0] != self.profile.executable:
            raise ValueError("Manifest executable differs from its declaration.")
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def canonical_root(self) -> Path:
        return self.profile.canonical_root

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


def build_manifest(
    project: Project, profile: ExecutionProfile, argv: tuple[str, ...]
) -> ActionManifest:
    if profile.project_id != project.project_id or profile.canonical_root != project.root:
        raise ValueError("Execution profile does not bind this project.")
    if not argv or argv[0] not in (profile.executable, PurePosixPath(profile.executable).name):
        raise ValueError("Executable is not declared by this profile.")
    return ActionManifest(profile=profile, argv=(profile.executable, *argv[1:]))


ExecutionOperation = Literal[
    "propose", "approve", "deny", "approval.show", "approval.list", "job.show", "job.list"
]


class ExecutionCall(_Model):
    request_id: UUID = Field(default_factory=uuid4)
    directory: Path
    expected_root: Path | None = None
    expected_project_id: UUID | None = None
    operation: ExecutionOperation
    profile_name: str = Field(default="default", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    argv: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    object_id: UUID | None = None
    after: StrictInt = Field(default=0, ge=0, le=9223372036854775807)
    limit: StrictInt = Field(default=16, ge=1, le=32)

    @field_validator("directory", "expected_root")
    @classmethod
    def path(cls, value: Path | None) -> Path | None:
        return absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.operation == "propose") != bool(self.argv):
            raise ValueError("Only proposals carry argv.")
        if (self.operation in {"approve", "deny", "approval.show", "job.show"}) != (
            self.object_id is not None
        ):
            raise ValueError("Only targeted operations carry an object ID.")
        if not self.operation.endswith(".list") and (self.after != 0 or self.limit != 16):
            raise ValueError("Only list operations carry pagination.")
        for value in self.argv:
            public_text(value)
        return self


class ReviewRecord(_Model):
    sequence: StrictInt = Field(gt=0)
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    manifest: ActionManifest
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ReviewState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    owner_instance_id: UUID
    job_state: Literal["waiting_for_approval", "blocked"]
    reason: Literal[
        "approval_required",
        "execution_unavailable",
        "approval_denied",
        "approval_expired",
        "approval_invalidated",
    ]
    execution_available: Literal[False] = False
    launch_authority_issued: Literal[False] = False

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.manifest_digest != self.manifest.fingerprint:
            raise ValueError("Stored manifest fingerprint does not match.")
        waiting = self.state == ReviewState.PENDING
        if waiting != (self.job_state == "waiting_for_approval"):
            raise ValueError("Review state does not match job disposition.")
        reasons = {
            ReviewState.PENDING: "approval_required",
            ReviewState.APPROVED: "execution_unavailable",
            ReviewState.DENIED: "approval_denied",
            ReviewState.EXPIRED: "approval_expired",
            ReviewState.INVALIDATED: "approval_invalidated",
        }
        if self.reason != reasons[self.state] or self.expires_at_ms < self.created_at_ms:
            raise ValueError("Review facts are inconsistent.")
        return self


class ExecutionData(_Model):
    items: tuple[ReviewRecord, ...] = Field(default=(), max_length=32)
    next_after: StrictInt | None = Field(default=None, gt=0)
    execution_available: Literal[False] = False
    message: Literal[
        "Review only: execution is unavailable. Fresh Phase3 approval is required."
    ] = "Review only: execution is unavailable. Fresh Phase3 approval is required."


class ExecutionSuccess(_Model):
    ok: Literal[True] = True
    data: ExecutionData
    error: None = None


ExecutionResult = ExecutionSuccess | Failure


class ExecutionReply(_Model):
    request_id: UUID
    identity: UUID | None = None
    result: ExecutionResult


class ReviewOperation(_Model):
    call: ExecutionCall
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    manifest: ActionManifest | None = None
    configuration_fingerprint: str | None = None


class ReviewFault(Exception):
    """Fixed review refusal, not a raw storage or validation exception."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReviewStore(Protocol):
    def review(self, operation: ReviewOperation, *, deadline: float) -> ExecutionData: ...


class ExecutionClient(Protocol):
    def review(self, call: ExecutionCall) -> ExecutionReply: ...


def execution_failure(code: str) -> Failure:
    messages = {
        "execution_profile_missing": "No declared execution profile binds this project.",
        "execution_invalid": "The public action or profile is invalid; no review was stored.",
        "execution_target_mismatch": "Execution declaration changed; no authority issued.",
        "execution_not_found": "No review record exists in this validated project scope.",
        "execution_conflict": "Review identity or decision conflicts; inspect existing facts.",
        "execution_expired": "The review expired or was invalidated; fresh review is required.",
        "execution_capacity": "The bounded review ledger is full; no additional review admitted.",
        "execution_clock": "Review clock is unconfirmed; no approval was granted.",
        "execution_outcome_unknown": "A prior launch is unconfirmed; new claims are quarantined.",
        "storage_busy": "Storage admission is busy; no automatic retry was made.",
        "storage_closing": "Storage is closing; inspect existing facts after restart.",
        "storage_outcome_unconfirmed": "Storage outcome is unknown; inspect rather than retry.",
        "storage_failed": "Storage failed; no automatic repair was attempted.",
    }
    safe = code if code in messages else "execution_invalid"
    return Failure(error=ErrorData(code=safe, message=messages[safe]))


class ExecutionService:
    def __init__(
        self,
        projects: ProjectService,
        store: ReviewStore,
        profiles: Callable[[], tuple[ExecutionProfile, ...]],
        instance_id: UUID,
    ) -> None:
        self.projects, self.store, self.profiles, self.instance_id = (
            projects,
            store,
            profiles,
            instance_id,
        )

    def execute(self, call: ExecutionCall, deadline: float) -> ExecutionReply:
        # ProjectService uses fixed internal discovery, never the proposed argv.
        observed = self.projects.execute("config", call.directory, deadline=deadline)
        if not observed.ok:
            return ExecutionReply(request_id=call.request_id, result=observed)
        project = Project(UUID(observed.data.project_id), Path(observed.data.root))
        if (call.expected_root is not None and call.expected_root != project.root) or (
            call.expected_project_id is not None and call.expected_project_id != project.project_id
        ):
            return ExecutionReply(
                request_id=call.request_id, result=execution_failure("execution_target_mismatch")
            )
        try:
            name = call.profile_name
            if call.operation == "approve":
                current = self.store.review(
                    ReviewOperation(
                        call=call.model_copy(update={"operation": "approval.show"}),
                        project_id=project.project_id,
                        canonical_root=project.root,
                        owner_instance_id=self.instance_id,
                    ),
                    deadline=deadline,
                )
                name = current.items[0].manifest.profile.name
            matches = [
                p
                for p in self.profiles()
                if p.name == name
                and p.project_id == project.project_id
                and p.canonical_root == project.root
            ]
            profile = matches[0] if len(matches) == 1 else None
            if call.operation == "propose" and profile is None:
                return ExecutionReply(
                    request_id=call.request_id,
                    result=execution_failure("execution_profile_missing"),
                )
            manifest = (
                build_manifest(project, profile, call.argv)
                if call.operation == "propose" and profile is not None
                else None
            )
            operation = ReviewOperation(
                call=call,
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=self.instance_id,
                manifest=manifest,
                configuration_fingerprint=profile.fingerprint if profile is not None else None,
            )
            data = self.store.review(operation, deadline=deadline)
            result: ExecutionResult = ExecutionSuccess(data=data)
        except ReviewFault as error:
            result = execution_failure(error.code)
        except ValueError:
            result = execution_failure("execution_invalid")
        return ExecutionReply(request_id=call.request_id, identity=self.instance_id, result=result)


class PreparedFile(_Model):
    path: str = Field(min_length=1, max_length=1023)
    size: StrictInt = Field(ge=0, le=PREPARED_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def safe_relative_file(cls, value: str) -> str:
        path = PurePosixPath(public_text(value, 1023))
        if (
            path.is_absolute()
            or ".." in path.parts
            or str(path) != value
            or value == "."
            or "\\" in value
            or protected_path(value)
            or any(character in value for character in "*?[]")
        ):
            raise ValueError("Only literal unprotected relative files are supported.")
        return value


class PreparedInputs(_Model):
    """Inventory assertions, not proof that bytes were safely copied or verified."""

    snapshot_id: UUID = Field(default_factory=uuid4)
    policy: Literal["literal_files_v1"] = "literal_files_v1"
    files: tuple[PreparedFile, ...] = Field(default=(), max_length=PREPARED_FILE_LIMIT)

    @model_validator(mode="after")
    def bounded_inventory(self) -> Self:
        names = [item.path for item in self.files]
        if names != sorted(names) or len({n.casefold() for n in names}) != len(names):
            raise ValueError("Inventory must be sorted and have no colliding paths.")
        if self.total_bytes > PREPARED_TOTAL_BYTES:
            raise ValueError("Prepared input byte budget exceeded.")
        return self

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def digest(self) -> str:
        return fingerprint({"policy": self.policy, "files": [f.model_dump() for f in self.files]})


class ExecutionManifest(_Model):
    """Fresh action identity; the trusted verifier must still check target and inputs.

    Not a subclass or reinterpretation of the permanently review-only ActionManifest.
    Public intent is separate; only the trusted target/input verifier grants fresh authority.
    """

    authority_kind: Literal["execution_v1"] = "execution_v1"
    verification: Literal["required_before_authority"] = "required_before_authority"
    profile: ExecutionProfile
    argv: tuple[str, ...] = Field(min_length=1, max_length=ARGV_COUNT)
    inputs: PreparedInputs

    @model_validator(mode="after")
    def coherent_action(self) -> Self:
        ActionManifest(profile=self.profile, argv=self.argv)
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class AuthorityOperation(_Model):
    """Trusted installation-internal port; current digest is a verifier obligation.

    Digest equality alone does not establish actual engine or input verification.
    The future verifier must check these before propose, approve and worker claim.
    """

    operation: Literal["propose", "approve", "deny", "show", "claim"]
    request_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    approval_id: UUID | None = None
    manifest: ExecutionManifest | None = None
    current_manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.operation == "propose") != (self.manifest is not None):
            raise ValueError("Only a fresh proposal carries a manifest.")
        if (self.operation != "propose") != (self.approval_id is not None):
            raise ValueError("Only targeted operations carry approval identity.")
        if (self.operation in {"propose", "approve", "claim"}) != (
            self.current_manifest_digest is not None
        ):
            raise ValueError("Authority changes require a current verifier binding.")
        return self


class AuthorityRecord(_Model):
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    manifest: ExecutionManifest
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AuthorityState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    claim_id: UUID | None = None
    claim_state: Literal["claimed", "unknown", "finished"] | None = None
    claimed_at_ms: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.manifest_digest != self.manifest.fingerprint
            or self.project_id != self.manifest.profile.project_id
            or self.canonical_root != self.manifest.profile.canonical_root
            or self.expires_at_ms < self.created_at_ms
        ):
            raise ValueError("Authority identity or manifest facts disagree.")
        consumed = self.state == AuthorityState.CONSUMED
        if any(
            (item is not None) != consumed
            for item in (self.claim_id, self.claim_state, self.claimed_at_ms)
        ):
            raise ValueError("Only consumed authority carries a complete launch claim.")
        if self.claimed_at_ms is not None and not (
            self.created_at_ms <= self.claimed_at_ms < self.expires_at_ms
        ):
            raise ValueError("Claim time must be within the original approval lifetime.")
        return self


class AuthorityResult(_Model):
    record: AuthorityRecord
    # First confirmed claim response only. Never a replayable public bearer token.
    launch_claim: UUID | None = None

    @model_validator(mode="after")
    def fresh_claim(self) -> Self:
        if self.launch_claim is not None and (
            self.launch_claim != self.record.claim_id or self.record.claim_state != "claimed"
        ):
            raise ValueError("Fresh claim does not match consumed authority.")
        return self


class ExecutionAdmission(_Model):
    """Confirmed claim acknowledgement, never a token or proof a guest started.

    Replays describe current persisted facts only. Delivery of this observation is
    independent of the lifetime of the worker that owns a first confirmed claim.
    """

    authority_kind: Literal["execution_v1"] = "execution_v1"
    request_id: UUID
    project_id: UUID
    canonical_root: Path
    approval_id: UUID
    job_id: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_state: Literal["claimed", "unknown", "finished"]
    replayed: StrictBool

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def first_claim(self) -> Self:
        if not self.replayed and self.claim_state != "claimed":
            raise ValueError("A first admission acknowledges a confirmed claim only.")
        return self


class ExecutionStartCall(_Model):
    """Explicit fresh start identity; reviewed digest is not a current-proof override."""

    authority_kind: Literal["execution_v1"] = "execution_v1"
    request_id: UUID = Field(default_factory=uuid4)
    directory: Path
    approval_id: UUID
    reviewed_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("directory")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)


class ExecutionStartReply(_Model):
    """Admission or refusal only; actual completion is inspected through durable facts."""

    request_id: UUID
    identity: UUID | None = None
    admission: ExecutionAdmission | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.admission is None) == (self.error is None):
            raise ValueError("Start replies require exactly one admission or failure.")
        if self.admission is not None and (
            self.identity is None or self.admission.request_id != self.request_id
        ):
            raise ValueError("Admission replies require matching identity and correlation.")
        return self


class AuthorityStore(Protocol):
    def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult: ...


class ProposalRequestScope(_Model):
    """Facts lookup identity, never preparation or launch permission."""

    request_id: UUID
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)


class ProposalLookupStore(Protocol):
    def lookup_proposal(
        self, scope: ProposalRequestScope, *, deadline: float
    ) -> AuthorityRecord | None: ...


class DockerTargetDeclaration(_Model):
    """Explicit public image defaults, separate from all persisted manifest schemas.

    These declarations are not automatic environment discovery or secret detection.
    Future creation must override image argv/user/cwd and verify the effective config.
    """

    profile: ExecutionProfile
    image_environment: tuple[tuple[str, str], ...] = Field(default=(), max_length=32)
    image_user: str = Field(default="", max_length=128)
    image_entrypoint: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    image_command: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    image_working_dir: str = Field(default="", max_length=1023)

    @model_validator(mode="after")
    def public_defaults(self) -> Self:
        names = [name for name, _ in self.image_environment]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Image environment must be sorted and unique.")
        for name, value in self.image_environment:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) or re.search(
                r"(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|API_?KEY|ACCESS_KEY|AUTHORIZATION)",
                name,
            ):
                raise ValueError("Sensitive environment channels are unsupported.")
            public_text(value)
        for value in (
            self.image_user,
            self.image_working_dir,
            *self.image_entrypoint,
            *self.image_command,
        ):
            public_text(value)
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class DockerCLIIdentity(_Model):
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: StrictInt = Field(gt=0, le=268435456)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: Path) -> Path:
        return absolute_path(value)


class DockerBackendConfiguration(_Model):
    """Explicit installation-private declaration, not verification or authority.

    Construction is cold: paths/identity/defaults must be supplied, never discovered.
    Actual ownership, permissions and binary/engine identity remain adapter checks.
    This standalone contract does not extend legacy settings or enable execution.
    """

    binary: DockerCLIIdentity
    config: Path
    declarations: tuple[DockerTargetDeclaration, ...] = Field(min_length=1, max_length=16)

    @field_validator("config")
    @classmethod
    def config_path(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def explicit_and_bounded(self) -> Self:
        if (
            self.config == self.binary.path
            or self.config in self.binary.path.parents
            or self.binary.path in self.config.parents
        ):
            raise ValueError("Private CLI and configuration paths must not overlap.")
        identities = [(d.profile.project_id, d.profile.name) for d in self.declarations]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate backend project/profile declaration.")
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class VerifiedDockerTarget(_Model):
    """Observed metadata, not isolation proof, input verification or launch authority."""

    declaration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binary: DockerCLIIdentity
    endpoint: str = Field(max_length=1023)
    engine_id: str = Field(min_length=1, max_length=128)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/arm64", "linux/amd64"]
    client_version: str = Field(max_length=64)
    server_version: str = Field(max_length=64)
    image_configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_environment: tuple[tuple[str, str], ...] = Field(max_length=35)
    isolation_verified: Literal[False] = False
    inputs_verified: Literal[False] = False
    launch_authority_issued: Literal[False] = False

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class DockerFailureFacts(_Model):
    """Bounded allowlisted failure metadata; never raw CLI text or arbitrary field names."""

    returncode: StrictInt = Field(ge=-255, le=255)
    stdout_bytes: StrictInt = Field(ge=0, le=65536)
    stderr_bytes: StrictInt = Field(ge=0, le=65536)
    category: Literal["template_field_unavailable", "stderr_on_success", "other_cli_failure"]
    field_name: (
        Literal[
            "ID",
            "Id",
            "OS",
            "Os",
            "Architecture",
            "Variant",
            "Config",
            "Env",
            "User",
            "Entrypoint",
            "Cmd",
            "WorkingDir",
            "Volumes",
            "Healthcheck",
            "OnBuild",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.stdout_bytes + self.stderr_bytes > 65536 or (
            (self.category == "template_field_unavailable") != (self.field_name is not None)
        ):
            raise ValueError("Inconsistent bounded Docker failure metadata.")
        return self


DockerPolicyCheckId = Literal[
    "Config",
    "HostConfig",
    "Config.User",
    "Config.WorkingDir",
    "Config.Entrypoint",
    "Config.Cmd",
    "Config.Labels",
    "Config.StopSignal",
    "Config.OpenStdin",
    "Config.AttachStdin",
    "Config.Tty",
    "Config.Healthcheck",
    "Config.Env.type",
    "Config.Env.values",
    "Image",
    "Path",
    "Args",
    "HostConfig.NetworkMode",
    "HostConfig.IpcMode",
    "HostConfig.CgroupnsMode",
    "HostConfig.Memory",
    "HostConfig.MemorySwap",
    "HostConfig.NanoCpus",
    "HostConfig.PidsLimit",
    "HostConfig.ShmSize",
    "HostConfig.CapDrop",
    "HostConfig.Tmpfs",
    "HostConfig.ReadonlyRootfs",
    "HostConfig.Privileged",
    "HostConfig.AutoRemove",
    "HostConfig.PublishAllPorts",
    "HostConfig.CapAdd",
    "HostConfig.Devices",
    "HostConfig.DeviceRequests",
    "HostConfig.DeviceCgroupRules",
    "HostConfig.PortBindings",
    "HostConfig.Links",
    "HostConfig.VolumesFrom",
    "HostConfig.ExtraHosts",
    "HostConfig.Binds",
    "HostConfig.PidMode",
    "HostConfig.UTSMode",
    "HostConfig.Runtime",
    "HostConfig.SecurityOpt",
    "HostConfig.RestartPolicy",
    "HostConfig.RestartPolicy.Name",
    "HostConfig.LogConfig",
    "HostConfig.LogConfig.Type",
    "Mounts.type",
    "Mounts.count",
    "Mounts.entry",
    "Mounts.project.Type",
    "Mounts.project.Source",
    "Mounts.project.RW",
    "Mounts.project.Propagation",
    "Mounts.scratch.Type",
    "Mounts.scratch.RW",
    "State",
    "State.Status",
    "State.Running",
]


class DockerPolicyFacts(_Model):
    """First rejected predicate only: fixed identifier and booleans, never raw values.

    `type_ok` reports the expected structural kind, not acceptance. `matches` is
    always false; neither these facts nor synthetic fixtures establish live cause.
    """

    check_id: DockerPolicyCheckId
    present: StrictBool
    type_ok: StrictBool
    matches: StrictBool = False

    @model_validator(mode="after")
    def rejected(self) -> Self:
        if self.matches:
            raise ValueError("Policy failure facts must describe a rejected predicate.")
        return self


class DockerBackendFault(Exception):
    """Fixed non-secret refusal; never attach raw CLI output or parser exceptions."""

    def __init__(
        self,
        code: Literal[
            "docker_unavailable",
            "docker_unsafe_configuration",
            "docker_target_mismatch",
            "docker_unsupported",
            "docker_protocol_error",
            "docker_output_limit",
            "docker_output_failed",
            "docker_cancelled",
            "docker_timeout",
            "docker_command_failed",
            "docker_cleanup_unconfirmed",
            "docker_busy",
            "docker_authority_required",
            "docker_ownership_unconfirmed",
            "docker_policy_mismatch",
            "docker_mutation_unconfirmed",
            "docker_journal_unconfirmed",
            "docker_home_capacity",
        ],
        *,
        diagnostics: DockerFailureFacts | None = None,
        policy: DockerPolicyFacts | None = None,
        observed_container_id: str | None = None,
    ) -> None:
        if (
            observed_container_id is not None
            and re.fullmatch(r"[0-9a-f]{64}", observed_container_id) is None
        ):
            raise ValueError("Unconfirmed container identity must be bounded.")
        if policy is not None and code != "docker_policy_mismatch":
            raise ValueError("Policy facts require a policy refusal.")
        self.code = code
        self.policy = policy
        self.diagnostics = diagnostics
        self.observed_container_id = observed_container_id
        super().__init__(code)


class DockerTargetVerifier(Protocol):
    def verify_target(
        self, declaration: DockerTargetDeclaration, *, deadline: float
    ) -> VerifiedDockerTarget: ...


class PreparedInputBinding(_Model):
    """Trusted verifier's live binding, not permission to mount an arbitrary client path."""

    snapshot_id: UUID
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: Path

    @field_validator("path")
    @classmethod
    def mount_path(cls, value: Path) -> Path:
        absolute_path(value)
        if any(character in str(value) for character in ',"'):
            raise ValueError("Ambiguous mount-path encoding is unsupported.")
        return value


class PreparedInputFault(Exception):
    """Fixed public-safe refusal; never include source paths, bytes or OS diagnostics."""

    def __init__(
        self,
        code: Literal[
            "input_invalid",
            "input_unsafe",
            "input_changed",
            "input_limit",
            "input_capacity",
            "input_timeout",
            "input_busy",
            "input_unsealed",
            "input_scope_mismatch",
            "input_unconfirmed",
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


def canonical_prepared_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Pure shared lexical validation, before source access or proposal replay."""
    if len(paths) > PREPARED_FILE_LIMIT:
        raise PreparedInputFault("input_limit")
    placeholders = tuple(PreparedFile(path=p, size=0, sha256="0" * 64) for p in sorted(paths))
    _ = PreparedInputs(files=placeholders).digest
    nodes: dict[tuple[str, ...], tuple[str, ...]] = {}
    leaves: set[tuple[str, ...]] = set()
    for item in placeholders:
        parts = PurePosixPath(item.path).parts
        for index in range(1, len(parts) + 1):
            literal = parts[:index]
            key = tuple(unicodedata.normalize("NFC", p).casefold() for p in literal)
            if key in nodes and nodes[key] != literal:
                raise PreparedInputFault("input_invalid")
            if index < len(parts) and key in leaves:
                raise PreparedInputFault("input_invalid")
            nodes[key] = literal
        key = tuple(unicodedata.normalize("NFC", p).casefold() for p in parts)
        if any(len(old) > len(key) and old[: len(key)] == key for old in nodes):
            raise PreparedInputFault("input_invalid")
        leaves.add(key)
    return tuple(item.path for item in placeholders)


class PreparedInputPreparer(Protocol):
    def prepare(
        self, project: Project, paths: tuple[str, ...], *, deadline: float
    ) -> PreparedInputs: ...


class PreparedInputVerifier(Protocol):
    def verify(self, manifest: ExecutionManifest, *, deadline: float) -> PreparedInputBinding: ...
    def check(
        self, binding: PreparedInputBinding, manifest: ExecutionManifest, *, deadline: float
    ) -> None: ...


class DockerLaunchIntent(_Model):
    """Durable facts only: deliberately excludes transient AuthorityResult.launch_claim."""

    attempt_id: UUID
    record: AuthorityRecord
    target: VerifiedDockerTarget
    inputs: PreparedInputBinding
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def bound(self) -> Self:
        profile = self.record.manifest.profile
        if (
            self.record.state != AuthorityState.CONSUMED
            or self.record.claim_state != "claimed"
            or self.target.profile_digest != profile.fingerprint
            or self.target.engine_id != profile.engine_id
            or self.target.endpoint != profile.endpoint
            or self.target.image_id != profile.image_id
            or self.target.platform != profile.platform
            or self.inputs.snapshot_id != self.record.manifest.inputs.snapshot_id
            or self.inputs.inventory_digest != self.record.manifest.inputs.digest
        ):
            raise ValueError("Launch intent is not bound to consumed fresh authority.")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "attempt": str(self.attempt_id),
                "claim": str(self.record.claim_id),
                "job": str(self.record.job_id),
                "owner": str(self.record.owner_instance_id),
                "manifest": self.record.manifest_digest,
                "target": self.target.fingerprint,
                "inputs": self.inputs.model_dump(mode="json"),
                "nonce": self.nonce,
            }
        )


class DockerOwnedContainer(_Model):
    """Observation only. Controller-minted object identity is required for live control.

    Deserializing this model never adopts a container or reconstructs launch authority.
    """

    intent: DockerLaunchIntent
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=20, max_length=64, pattern=r"^[0-9TZ:.+\-]+$")


class DockerLaunchEvent(_Model):
    intent: DockerLaunchIntent
    phase: Literal["create_intent", "created", "start_intent", "exited", "cleaned", "unknown"]
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: str | None = Field(
        default=None, min_length=20, max_length=64, pattern=r"^[0-9TZ:.+\-]+$"
    )
    # A captured create reply without verified birth/ownership is evidence only.
    observed_container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.container_id is None) != (self.created_at is None):
            raise ValueError("Owned identity and birth must be paired.")
        if self.phase == "create_intent" and (
            self.container_id is not None or self.observed_container_id is not None
        ):
            raise ValueError("Create intent cannot assert a container already exists.")
        if self.observed_container_id is not None and (
            self.phase != "unknown" or self.container_id is not None
        ):
            raise ValueError("Partial create replies are uncertainty observations only.")
        if self.phase not in {"create_intent", "unknown"} and self.container_id is None:
            raise ValueError("Observed lifecycle facts require exact container identity.")
        if self.phase not in {"create_intent", "unknown"} and self.created_at is None:
            raise ValueError("Observed lifecycle facts require creation identity.")
        if (self.phase == "exited") != (self.exit_code is not None):
            raise ValueError("Only observed exit carries an exit code.")
        return self


class DockerLaunchScope(_Model):
    project_id: UUID
    canonical_root: Path
    claim_id: UUID

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)


class DockerLaunchSnapshot(_Model):
    """Historical observations only; never a controller handle or replay permission."""

    intent: DockerLaunchIntent
    phases: tuple[
        Literal["create_intent", "created", "start_intent", "exited", "cleaned", "unknown"], ...
    ] = Field(max_length=6)
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: str | None = Field(
        default=None, min_length=20, max_length=64, pattern=r"^[0-9TZ:.+\-]+$"
    )
    observed_container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    claim_state: Literal["claimed", "unknown", "finished"]
    uncertain: bool

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (
            (self.container_id is None) != (self.created_at is None)
            or len(self.phases) != len(set(self.phases))
            or self.uncertain != (self.claim_state == "unknown" or "unknown" in self.phases)
            or (("exited" in self.phases) != (self.exit_code is not None))
        ):
            raise ValueError("Inconsistent durable lifecycle observations.")
        return self


class DockerLaunchJournal(Protocol):
    """Must durably validate transition/claim uniqueness before returning; ACK loss raises.

    Enforce current owner/scope/manifest binding, original expiry at create/start,
    and permanent one-attempt exclusion. Only the SQL owner implements durability;
    no default/fallback journal or public route is granted by this port. Cleanup facts
    alone do not release admission before completion/output integration exists.
    """

    def record(self, event: DockerLaunchEvent, *, deadline: float) -> None: ...


OutputGuardReason = Literal[
    "none", "sensitive_channel", "line_limit", "deferred_limit", "invalid_utf8"
]


class DockerOutputSnapshot(_Model):
    """Sanitized descriptive output, never completion or launch authority.

    Text is plain ASCII with LF only; visible escapes must not be interpreted again.
    EOF completeness is not controller/transport/cleanup confirmation.
    """

    policy: Literal["guarded_output_v1"] = "guarded_output_v1"
    retention_limit: StrictInt = Field(ge=1, le=1048576)
    stdout: str = Field(max_length=1048576)
    stderr: str = Field(max_length=1048576)
    retained_bytes: StrictInt = Field(ge=0, le=1048576)
    stdout_source_bytes: StrictInt = Field(ge=0, le=9223372036854775807)
    stderr_source_bytes: StrictInt = Field(ge=0, le=9223372036854775807)
    stdout_eof: StrictBool
    stderr_eof: StrictBool
    aborted: StrictBool
    truncated: StrictBool
    guard_reason: OutputGuardReason

    @model_validator(mode="after")
    def bounded_safe_text(self) -> Self:
        if (
            self.retained_bytes != len(self.stdout) + len(self.stderr)
            or self.retained_bytes > self.retention_limit
            or any(
                char != "\n" and not 32 <= ord(char) <= 126
                for text in (self.stdout, self.stderr)
                for char in text
            )
            or (not self.aborted and not (self.stdout_eof and self.stderr_eof))
        ):
            raise ValueError("Inconsistent bounded sanitized output.")
        return self

    @property
    def complete(self) -> bool:
        return self.stdout_eof and self.stderr_eof and not self.aborted


class DockerOutputSink(Protocol):
    """Trusted bounded, nonblocking memory-only filter. Never persist raw input chunks."""

    @property
    def retention_limit(self) -> int: ...

    def accept(self, stream: Literal["stdout", "stderr"], data: bytes) -> None: ...
    def finish(self, stream: Literal["stdout", "stderr"]) -> None: ...


class DockerJobOutcome(_Model):
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    timed_out: bool = False
    outcome_unknown: bool = False
    cleanup_verified: bool = False


class DockerOutputCapture(DockerOutputSink, Protocol):
    def seal(self, *, abort: bool = False) -> DockerOutputSnapshot: ...


class DockerOutputFacts(_Model):
    """Original capture facts survive logical text retirement; never authority."""

    policy: Literal["guarded_output_v1"]
    retention_limit: StrictInt = Field(ge=1, le=1048576)
    retained_bytes: StrictInt = Field(ge=0, le=1048576)
    stdout_source_bytes: StrictInt = Field(ge=0, le=9223372036854775807)
    stderr_source_bytes: StrictInt = Field(ge=0, le=9223372036854775807)
    stdout_eof: StrictBool
    stderr_eof: StrictBool
    aborted: StrictBool
    truncated: StrictBool
    guard_reason: OutputGuardReason

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.retained_bytes > self.retention_limit or (
            not self.aborted and not (self.stdout_eof and self.stderr_eof)
        ):
            raise ValueError("Inconsistent captured output facts.")
        return self


class DockerCompletion(_Model):
    """Trusted controller-to-SQL observation, not a public request or replay permit."""

    owned: DockerOwnedContainer
    outcome: DockerJobOutcome
    output: DockerOutputSnapshot

    @property
    def confirmed(self) -> bool:
        return (
            self.output.complete
            and not self.outcome.outcome_unknown
            and not self.outcome.timed_out
            and self.outcome.cleanup_verified
            and self.outcome.exit_code is not None
        )

    @property
    def output_facts(self) -> DockerOutputFacts:
        return DockerOutputFacts.model_validate(
            self.output.model_dump(exclude={"stdout", "stderr"})
        )

    @property
    def output_digest(self) -> str:
        # Unlike canonical metadata (32KiB), sanitized output can be1MiB.
        return sha256(self.output.model_dump_json().encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        return completion_digest(self.owned, self.outcome, self.output_facts, self.output_digest)


def completion_digest(
    owned: DockerOwnedContainer,
    outcome: DockerJobOutcome,
    facts: DockerOutputFacts,
    output_digest: str,
) -> str:
    return fingerprint(
        {
            "intent": owned.intent.fingerprint,
            "container": owned.container_id,
            "birth": owned.created_at,
            "outcome": outcome.model_dump(mode="json"),
            "facts": facts.model_dump(mode="json"),
            "output": output_digest,
        }
    )


class DockerExecutionResult(_Model):
    """Facts-only view; retired text is explicitly absent, not an empty capture."""

    owned: DockerOwnedContainer
    outcome: DockerJobOutcome
    output_facts: DockerOutputFacts
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["completed", "quarantined"]
    sequence: StrictInt = Field(ge=1, le=9223372036854775807)
    recorded_at_ms: StrictInt = Field(ge=0, le=9223372036854775807)
    payload_state: Literal["retained", "retired", "expired", "clock_unconfirmed"]
    retirement_reason: Literal["age", "capacity"] | None = None
    output: DockerOutputSnapshot | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.payload_state == "retained") != (
            self.output is not None
        ) or self.completion_digest != completion_digest(
            self.owned, self.outcome, self.output_facts, self.output_digest
        ):
            raise ValueError("Inconsistent completion facts.")
        if (self.payload_state == "retired") != (self.retirement_reason is not None):
            raise ValueError("Inconsistent payload retirement.")
        if self.disposition == "completed" and (
            self.outcome.outcome_unknown
            or self.outcome.timed_out
            or not self.outcome.cleanup_verified
            or self.outcome.exit_code is None
            or self.output_facts.aborted
            or not (self.output_facts.stdout_eof and self.output_facts.stderr_eof)
        ):
            raise ValueError("Unconfirmed completion.")
        if self.output is not None:
            observed = DockerCompletion(owned=self.owned, outcome=self.outcome, output=self.output)
            if (
                observed.output_digest != self.output_digest
                or observed.output_facts != self.output_facts
            ):
                raise ValueError("Inconsistent retained output.")
        return self


class DockerCompletionStore(Protocol):
    def complete_execution(
        self, completion: DockerCompletion, *, deadline: float
    ) -> DockerExecutionResult: ...


class DockerExecutionController(Protocol):
    """Owned lifecycle interface for internal orchestration, not public handle adoption."""

    def create(
        self,
        claim: AuthorityResult,
        declaration: DockerTargetDeclaration,
        *,
        deadline: float,
        cleanup_deadline: float,
    ) -> DockerOwnedContainer: ...
    def run(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        *,
        deadline: float,
        cleanup_deadline: float,
    ) -> DockerJobOutcome: ...
    def complete(
        self,
        handle: DockerOwnedContainer,
        capture: DockerOutputCapture,
        store: DockerCompletionStore,
        *,
        deadline: float,
    ) -> DockerExecutionResult: ...
    def terminate_remove_owned(self, handle: DockerOwnedContainer, *, deadline: float) -> bool: ...


class DockerLifecycleBackend(Protocol):
    """Installation-internal Docker I/O seam; never caller-supplied CLI arguments."""

    identity: DockerCLIIdentity

    def engine_id(self, endpoint: str, *, deadline: float) -> str: ...
    def create(self, intent: DockerLaunchIntent, *, deadline: float) -> str: ...
    def inspect(
        self, endpoint: str, container_id: str, *, deadline: float
    ) -> dict[str, object]: ...
    def attach(
        self, endpoint: str, container_id: str, sink: DockerOutputSink, *, deadline: float
    ) -> int: ...
    def stop(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def kill(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def remove(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def absent(self, endpoint: str, container_id: str, *, deadline: float) -> bool: ...


class ExecutionStarter(Protocol):
    """Owned-worker seam, not a callable endpoint or a caller-lifetime executor."""

    def start(
        self,
        directory: Path,
        approval_id: UUID,
        expected_manifest_digest: str,
        *,
        request_id: UUID,
        admission_deadline: float,
        on_admitted: Callable[[ExecutionAdmission], None],
    ) -> "DockerExecutionResult | AuthorityRecord | ExecutionStopRecord": ...


ExecutionLivePhase = Literal["claim_pending", "admitted", "running_observed", "stop_requested"]
FreshExecutionOperation = Literal["propose", "approve", "deny", "start", "show", "logs"]


class FreshExecutionCall(_Model):
    """Public intent only; no current proof, backend settings or launch authority."""

    authority_kind: Literal["execution_v1"] = "execution_v1"
    request_id: UUID
    directory: Path
    operation: FreshExecutionOperation
    profile_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    argv: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    paths: tuple[str, ...] = Field(default=(), max_length=PREPARED_FILE_LIMIT)
    approval_id: UUID | None = None
    reviewed_manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("directory")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.operation == "propose":
            if (
                self.profile_name is None
                or not self.argv
                or self.approval_id is not None
                or self.reviewed_manifest_digest is not None
            ):
                raise ValueError("Proposals require only a profile, literal argv and inputs.")
            for argument in self.argv:
                public_text(argument, ARGV_BYTES)
            try:
                canonical_prepared_paths(self.paths)
            except PreparedInputFault:
                raise ValueError("Invalid literal input inventory.") from None
        elif self.profile_name is not None or self.argv or self.paths or self.approval_id is None:
            raise ValueError(
                "Targeted operations require approval identity without new action inputs."
            )
        if (self.operation in {"approve", "deny", "start"}) != (
            self.reviewed_manifest_digest is not None
        ):
            raise ValueError("Decisions and starts require the exact reviewed digest only.")
        fingerprint(self.model_dump(mode="json"))
        return self


class FreshExecutionView(_Model):
    """Original reviewable facts, excluding private claim/owner/controller identities."""

    authority_kind: Literal["execution_v1"] = "execution_v1"
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    project_id: UUID
    canonical_root: Path
    manifest: ExecutionManifest
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AuthorityState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    claim_state: Literal["claimed", "unknown", "finished"] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.manifest_digest != self.manifest.fingerprint
            or self.project_id != self.manifest.profile.project_id
            or self.canonical_root != self.manifest.profile.canonical_root
            or self.expires_at_ms < self.created_at_ms
            or (self.state == AuthorityState.CONSUMED) != (self.claim_state is not None)
        ):
            raise ValueError("Inconsistent original execution facts.")
        return self

    @classmethod
    def from_record(cls, record: AuthorityRecord) -> Self:
        return cls.model_validate(
            {
                name: getattr(record, name)
                for name in (
                    "proposal_id",
                    "approval_id",
                    "job_id",
                    "project_id",
                    "canonical_root",
                    "manifest",
                    "manifest_digest",
                    "state",
                    "created_at_ms",
                    "expires_at_ms",
                    "claim_state",
                )
            }
        )


class FreshExecutionCompletion(_Model):
    """Durable result projection without owned handles or host/container identifiers."""

    disposition: Literal["completed", "quarantined"]
    outcome: DockerJobOutcome
    output_facts: DockerOutputFacts
    payload_state: Literal["retained", "retired", "expired", "clock_unconfirmed"]
    retirement_reason: Literal["age", "capacity"] | None = None
    recorded_at_ms: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.payload_state == "retired") != (self.retirement_reason is not None):
            raise ValueError("Inconsistent payload retirement facts.")
        if self.disposition == "completed" and (
            self.outcome.outcome_unknown
            or self.outcome.timed_out
            or not self.outcome.cleanup_verified
            or self.outcome.exit_code is None
            or self.output_facts.aborted
            or not (self.output_facts.stdout_eof and self.output_facts.stderr_eof)
        ):
            raise ValueError("Unconfirmed completion cannot be called completed.")
        return self


class ExecutionCancelCall(_Model):
    """Authenticated stop intent targets immutable reviewed identity, never live paths."""

    request_id: UUID
    approval_id: UUID
    reviewed_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionStopView(_Model):
    """Wire12 stop facts only: never a controller identity or complete-log promise."""

    request_id: UUID
    reason: Literal["cancel", "timeout", "shutdown"]
    requested_at_ms: StrictInt = Field(ge=0)
    clock_confirmed: StrictBool
    outcome: Literal["cancelled", "timed_out", "completed", "unknown"] | None = None
    evidence: Literal["not_started", "owned", "uncertain"] | None = None
    cleanup_verified: StrictBool | None = None
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    output_facts: DockerOutputFacts | None = None
    payload_state: Literal[
        "not_inspected", "missing", "retained", "retired", "expired", "clock_unconfirmed"
    ] = "not_inspected"
    retirement_reason: Literal["age", "capacity"] | None = None
    recorded_at_ms: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.outcome is None) != (self.evidence is None) or (
            (self.outcome is None) != (self.cleanup_verified is None)
        ):
            raise ValueError("Incomplete stop observation.")
        if self.outcome is None and (self.exit_code is not None or self.output_facts is not None):
            raise ValueError("Pending intent cannot assert terminal capture facts.")
        if self.outcome not in {None, "unknown"} and self.cleanup_verified is not True:
            raise ValueError("Known termination requires confirmed cleanup.")
        if self.evidence == "uncertain" and self.outcome != "unknown":
            raise ValueError("Uncertain evidence cannot confirm termination.")
        if self.evidence == "not_started" and (
            self.outcome not in {"cancelled", "timed_out"}
            or self.exit_code is not None
            or self.output_facts is not None
        ):
            raise ValueError("No-launch proof cannot invent execution facts.")
        if self.outcome in {"cancelled", "timed_out"} and self.outcome != (
            "timed_out" if self.reason == "timeout" else "cancelled"
        ):
            raise ValueError("Termination differs from the first stop reason.")
        if self.outcome == "completed" and (
            self.evidence != "owned"
            or self.exit_code is None
            or self.output_facts is None
            or self.output_facts.aborted
            or not (self.output_facts.stdout_eof and self.output_facts.stderr_eof)
        ):
            raise ValueError("Ordinary completion needs complete, confirmed capture facts.")
        if (self.payload_state == "retired") != (self.retirement_reason is not None):
            raise ValueError("Inconsistent stop payload retirement.")
        if self.payload_state not in {"missing", "not_inspected"} and (
            self.output_facts is None or self.recorded_at_ms is None
        ):
            raise ValueError("Stop payload availability requires capture facts.")
        return self

    @classmethod
    def from_record(cls, record: "ExecutionStopRecord", **payload: object) -> Self:
        fields: dict[str, object] = dict(
            request_id=record.request_id,
            reason=record.reason,
            requested_at_ms=record.requested_at_ms,
            clock_confirmed=record.clock_confirmed,
        )
        terminal = record.terminal
        if terminal is not None:
            fields.update(
                outcome=terminal.outcome,
                evidence=terminal.evidence,
                cleanup_verified=terminal.cleanup_verified,
                exit_code=terminal.exit_code,
                output_facts=terminal.output_facts,
            )
        return cls.model_validate({**fields, **payload})


class ExecutionCancelReply(_Model):
    request_id: UUID
    approval_id: UUID
    reviewed_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity: UUID | None = None
    ok: StrictBool
    stop: ExecutionStopView | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.ok != (self.error is None) or self.ok != (self.stop is not None):
            raise ValueError("Cancellation accepts durable intent only, not invented termination.")
        if self.ok and self.identity is None:
            raise ValueError("Accepted stop requires authenticated instance correlation.")
        return self


class ExecutionCancelClient(Protocol):
    def cancel(self, call: ExecutionCancelCall) -> ExecutionCancelReply: ...


class FreshExecutionReply(_Model):
    authority_kind: Literal["execution_v1"] = "execution_v1"
    request_id: UUID
    operation: FreshExecutionOperation
    identity: UUID | None = None
    ok: StrictBool
    view: FreshExecutionView | None = None
    admission: ExecutionAdmission | None = None
    completion: FreshExecutionCompletion | None = None
    completion_state: (
        Literal["not_claimed", "claim_only", "launch_without_result", "recorded", "stop_recorded"]
        | None
    ) = None
    stop: ExecutionStopView | None = None
    execution_phase: ExecutionLivePhase | Literal["finished", "unconfirmed"] | None = None
    output: DockerOutputSnapshot | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.ok != (self.error is None):
            raise ValueError("Success and failure disagree.")
        if not self.ok:
            if any(
                value is not None
                for value in (
                    self.view,
                    self.admission,
                    self.completion,
                    self.completion_state,
                    self.stop,
                    self.execution_phase,
                    self.output,
                )
            ):
                raise ValueError("Refusals cannot carry success facts.")
        elif self.identity is None:
            raise ValueError("Success requires authenticated instance correlation.")
        elif self.operation == "start":
            if (
                self.admission is None
                or self.admission.request_id != self.request_id
                or any(
                    value is not None
                    for value in (
                        self.view,
                        self.completion,
                        self.completion_state,
                        self.stop,
                        self.execution_phase,
                        self.output,
                    )
                )
            ):
                raise ValueError("Start acknowledges only the matching committed claim.")
        elif self.view is None or self.admission is not None:
            raise ValueError("Non-start success requires original scoped facts.")
        if (
            self.completion is not None
            or self.completion_state is not None
            or self.stop is not None
            or self.execution_phase is not None
        ) and self.operation not in {"show", "logs"}:
            raise ValueError("Completion is inspection only.")
        if self.ok and self.operation in {"show", "logs"}:
            if self.completion_state is None or (self.completion_state == "recorded") != (
                self.completion is not None
            ):
                raise ValueError("Inspection requires explicit consistent completion availability.")
            assert self.view is not None
            if self.completion is not None:
                expected = "finished" if self.completion.disposition == "completed" else "unknown"
                if self.view.claim_state != expected:
                    raise ValueError("Inspection authority and completion disagree.")
            elif self.completion_state == "stop_recorded":
                if self.stop is None or self.stop.outcome in {None, "completed"}:
                    raise ValueError("Stop inspection requires a distinct terminal.")
                if self.view.claim_state is None:
                    if (
                        self.stop.evidence != "not_started"
                        or self.view.state != AuthorityState.INVALIDATED
                    ):
                        raise ValueError("Unclaimed cancellation requires positive revocation.")
                elif self.view.claim_state != (
                    "unknown" if self.stop.outcome == "unknown" else "finished"
                ):
                    raise ValueError("Stop terminal and authority disagree.")
            elif self.view.claim_state == "finished" or (
                self.completion_state == "not_claimed"
            ) != (self.view.claim_state is None):
                raise ValueError("Inconsistent absent completion facts.")
            payload = self.completion if self.completion is not None else self.stop
            if self.operation == "logs" and (
                (payload is not None and payload.payload_state == "retained")
                != (self.output is not None)
            ):
                raise ValueError("Log text must agree with explicit retention state.")
        if self.output is not None:
            payload = self.completion if self.completion is not None else self.stop
            if self.operation != "logs" or payload is None or payload.payload_state != "retained":
                raise ValueError("Only logs may carry retained guarded text.")
            if (
                DockerOutputFacts.model_validate(
                    self.output.model_dump(exclude={"stdout", "stderr"})
                )
                != payload.output_facts
            ):
                raise ValueError("Guarded text disagrees with capture facts.")
        return self


def fresh_execution_failure(code: str) -> Failure:
    messages = {
        "execution_unavailable": "Fresh execution is not enabled for this runtime or scope.",
        "execution_busy": "An owned execution or serialized action is still active.",
        "execution_backend_unavailable": (
            "Configured fresh backend failed identity/private-resource checks; it is disabled."
        ),
        "execution_backend_capacity": (
            "Private backend generation capacity is full; no cleanup or reuse was attempted."
        ),
        "execution_conflict": "Request identity or reviewed action conflicts with stored facts.",
        "execution_expired": "Approval expired or was invalidated; a fresh review is required.",
        "execution_not_found": "No fresh execution record exists in this validated scope.",
        "request_outcome_unknown": (
            "Request outcome is unconfirmed; inspect original facts before another action."
        ),
        "execution_unconfirmed": (
            "Fresh execution facts or outcome are unconfirmed; inspect before further action."
        ),
    }
    aliases = {
        "docker_busy": "execution_busy",
        "docker_authority_required": "execution_unavailable",
        "runtime_capability_unavailable": "execution_unavailable",
        "storage_outcome_unconfirmed": "request_outcome_unknown",
    }
    safe = aliases.get(code, code)
    if safe not in messages:
        safe = "execution_unconfirmed"
    return Failure(error=ErrorData(code=safe, message=messages[safe]))


class FreshExecutionClient(Protocol):
    def fresh(self, call: FreshExecutionCall) -> FreshExecutionReply: ...


class FreshExecutionService(ExecutionStarter, Protocol):
    def execute(self, call: FreshExecutionCall, deadline: float) -> FreshExecutionReply: ...


class ExecutionInspectionScope(_Model):
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    approval_id: UUID

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)


class ExecutionInspection(_Model):
    record: AuthorityRecord
    completion_state: Literal[
        "not_claimed", "claim_only", "launch_without_result", "recorded", "stop_recorded"
    ]
    result: DockerExecutionResult | None = None
    stop: ExecutionStopView | None = None
    stop_output: DockerOutputSnapshot | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.completion_state == "stop_recorded":
            if (
                self.stop is None
                or self.stop.outcome in {None, "completed"}
                or self.result is not None
            ):
                raise ValueError("Stop terminal cannot manufacture ordinary completion.")
            if self.record.claim_id is None:
                if (
                    self.stop.evidence != "not_started"
                    or self.record.state != AuthorityState.INVALIDATED
                ):
                    raise ValueError("Unclaimed stop requires positive revocation.")
            elif self.record.claim_state != (
                "unknown" if self.stop.outcome == "unknown" else "finished"
            ):
                raise ValueError("Stop and consumed authority disagree.")
            if (self.stop.payload_state == "retained") != (self.stop_output is not None):
                raise ValueError("Stop payload disagrees with availability.")
            if (
                self.stop_output is not None
                and DockerOutputFacts.model_validate(
                    self.stop_output.model_dump(exclude={"stdout", "stderr"})
                )
                != self.stop.output_facts
            ):
                raise ValueError("Stop capture facts disagree.")
            return self
        if self.stop_output is not None:
            raise ValueError("Ordinary results use the original payload only.")
        if (self.completion_state == "not_claimed") != (self.record.claim_id is None):
            raise ValueError("Completion availability disagrees with claim facts.")
        if (self.completion_state == "recorded") != (self.result is not None):
            raise ValueError("Inconsistent completion availability.")
        if self.result is not None:
            original = self.result.owned.intent.record
            if (original.claim_id, original.approval_id, original.manifest_digest) != (
                self.record.claim_id,
                self.record.approval_id,
                self.record.manifest_digest,
            ):
                raise ValueError("Completion belongs to different authority.")
            expected = "finished" if self.result.disposition == "completed" else "unknown"
            if self.record.claim_state != expected:
                raise ValueError("Authority and result disagree.")
        elif self.record.claim_state == "finished":
            raise ValueError("Finished authority requires a confirmed result.")
        return self


class ExecutionResultReader(Protocol):
    def inspect_execution(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionInspection: ...


class ExecutionStopRequest(ExecutionInspectionScope):
    """Internal stop intent, not a signal, completion proof or public launch grant."""

    request_id: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["cancel", "timeout", "shutdown"] = "cancel"


class ExecutionStopTerminal(_Model):
    """Versioned terminal facts; known termination does not imply complete logs."""

    outcome: Literal["cancelled", "timed_out", "completed", "unknown"]
    evidence: Literal["not_started", "owned", "uncertain"]
    cleanup_verified: StrictBool
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    attempt_id: UUID | None = None
    ownership_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_facts: DockerOutputFacts | None = None
    output_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent(self) -> Self:
        owned = self.evidence == "owned"
        if owned != (self.attempt_id is not None and self.ownership_digest is not None):
            raise ValueError("Owned stop evidence requires exact identity facts.")
        if not owned and (self.attempt_id is not None or self.ownership_digest is not None):
            raise ValueError("Unowned stop evidence cannot invent a container identity.")
        if (self.output_facts is None) != (self.output_digest is None):
            raise ValueError("Output facts and digest must agree.")
        if self.outcome != "unknown" and not self.cleanup_verified:
            raise ValueError("Known stop requires verified cleanup.")
        if self.evidence == "not_started" and (
            self.outcome not in {"cancelled", "timed_out"}
            or self.exit_code is not None
            or self.output_facts is not None
        ):
            raise ValueError("A prelaunch stop cannot invent execution or output.")
        if self.evidence == "uncertain" and self.outcome != "unknown":
            raise ValueError("Uncertain evidence cannot confirm termination.")
        if self.outcome == "completed" and (not owned or self.exit_code is None):
            raise ValueError("Ordinary completion requires observed owned exit.")
        return self


class ExecutionStopRecord(_Model):
    """Durable stop facts, never a restartable timer or reusable control handle.

    The first request fixes UTC observation and policy budgets. A live owner must
    cap its monotonic deadlines by the original job and shutdown deadlines; loading
    these facts never starts another timer or reconciles unknown work.
    """

    version: Literal[1] = 1
    scope: ExecutionInspectionScope
    request_id: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["cancel", "timeout", "shutdown"]
    requested_at_ms: StrictInt = Field(ge=0, le=9223372036854760807)
    clock_confirmed: StrictBool
    grace_seconds: Literal[5] = 5
    cleanup_seconds: Literal[10] = 10
    completion_seconds: Literal[5] = 5
    terminal: ExecutionStopTerminal | None = None

    @model_validator(mode="after")
    def reason_matches(self) -> Self:
        if self.terminal is not None and self.terminal.outcome in {"cancelled", "timed_out"}:
            expected = "timed_out" if self.reason == "timeout" else "cancelled"
            if self.terminal.outcome != expected:
                raise ValueError("Terminal observation disagrees with first stop reason.")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class ExecutionStopObservation(_Model):
    """Trusted owner-to-SQL facts; constructed models alone do not prove no launch."""

    scope: ExecutionInspectionScope
    request_id: UUID
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal: ExecutionStopTerminal
    owned: DockerOwnedContainer | None = None

    @model_validator(mode="after")
    def bound(self) -> Self:
        if (self.terminal.evidence == "owned") != (self.owned is not None):
            raise ValueError("Stop observation requires matching ownership evidence.")
        if self.owned is not None:
            intent = self.owned.intent
            expected = fingerprint(
                {
                    "intent": intent.fingerprint,
                    "container": self.owned.container_id,
                    "birth": self.owned.created_at,
                }
            )
            if (
                self.terminal.attempt_id != intent.attempt_id
                or self.terminal.ownership_digest != expected
                or self.manifest_digest != intent.record.manifest_digest
                or self.scope.approval_id != intent.record.approval_id
                or self.scope.project_id != intent.record.project_id
                or self.scope.canonical_root != intent.record.canonical_root
                or self.scope.owner_instance_id != intent.record.owner_instance_id
            ):
                raise ValueError("Stop observation belongs to different owned work.")
        return self


class ExecutionStopCapture(_Model):
    """One atomic owner-to-SQL stop plus already sealed, guarded capture."""

    observation: ExecutionStopObservation
    output: DockerOutputSnapshot

    @model_validator(mode="after")
    def bound(self) -> Self:
        terminal = self.observation.terminal
        facts = DockerOutputFacts.model_validate(
            self.output.model_dump(exclude={"stdout", "stderr"})
        )
        if (
            terminal.outcome == "completed"
            or terminal.output_facts != facts
            or (
                terminal.output_digest
                != sha256(self.output.model_dump_json().encode("utf-8")).hexdigest()
            )
        ):
            raise ValueError("Stop capture differs from immutable terminal facts.")
        return self


class ExecutionStopSignal:
    """Private live-owner latch. Stored observations cannot construct or restart its timers."""

    def __init__(self, authority: AuthorityRecord, *, job_deadline: float) -> None:
        if not math.isfinite(job_deadline):
            raise ValueError("A finite original deadline is required.")
        self.authority = authority
        self.scope = ExecutionInspectionScope(
            project_id=authority.project_id,
            canonical_root=authority.canonical_root,
            owner_instance_id=authority.owner_instance_id,
            approval_id=authority.approval_id,
        )
        self._lock = threading.Lock()
        self._request: ExecutionStopRequest | None = None
        self._confirmed: ExecutionStopRecord | None = None
        self._running = False
        self._claimed = authority.state == AuthorityState.CONSUMED
        self._completion_known = False
        self._grace = job_deadline + 5
        self._cleanup = job_deadline + 10
        self._recording = job_deadline + 15

    @property
    def completion_known(self) -> bool:
        with self._lock:
            return self._completion_known

    def finished(self, result: ExecutionStopRecord | DockerExecutionResult) -> None:
        if isinstance(result, DockerExecutionResult):
            self.require_scope(result.owned.intent.record)
            known = result.disposition == "completed"
        else:
            if (
                result.scope != self.scope
                or result.manifest_digest != self.authority.manifest_digest
            ):
                raise DockerBackendFault("docker_authority_required")
            known = (
                result.terminal is not None
                and result.terminal.outcome != "unknown"
                and result.terminal.cleanup_verified
            )
        with self._lock:
            self._completion_known = known

    def require_scope(self, authority: AuthorityRecord) -> None:
        if (
            authority.approval_id,
            authority.project_id,
            authority.canonical_root,
            authority.owner_instance_id,
            authority.manifest_digest,
        ) != (
            self.scope.approval_id,
            self.scope.project_id,
            self.scope.canonical_root,
            self.scope.owner_instance_id,
            self.authority.manifest_digest,
        ):
            raise DockerBackendFault("docker_authority_required")

    def request(
        self,
        request_id: UUID,
        reason: Literal["cancel", "timeout", "shutdown"],
        *,
        shutdown_deadline: float | None = None,
    ) -> None:
        request = ExecutionStopRequest(
            **self.scope.model_dump(),
            request_id=request_id,
            manifest_digest=self.authority.manifest_digest,
            reason=reason,
        )
        if shutdown_deadline is not None and not math.isfinite(shutdown_deadline):
            raise ValueError("A finite shutdown cap is required.")
        with self._lock:
            if self._request is None:
                now = time.monotonic()
                self._request = request
                self._grace = min(self._grace, now + 5)
                self._cleanup = min(self._cleanup, now + 10)
                self._recording = min(self._recording, now + 15)
            if shutdown_deadline is not None:
                self._recording = min(self._recording, shutdown_deadline)
                self._cleanup = min(self._cleanup, shutdown_deadline - 5)
            self._grace = min(self._grace, self._cleanup)

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._request is not None

    @property
    def phase(self) -> ExecutionLivePhase:
        with self._lock:
            return (
                "stop_requested"
                if self._request is not None
                else (
                    "running_observed"
                    if self._running
                    else "admitted"
                    if self._claimed
                    else "claim_pending"
                )
            )

    def mark_claimed(self) -> None:
        with self._lock:
            self._claimed = True

    def mark_running(self) -> None:
        with self._lock:
            self._running = True

    def check(self) -> None:
        if self.requested:
            raise DockerBackendFault("docker_cancelled")

    def request_record(self) -> ExecutionStopRequest:
        with self._lock:
            if self._request is None:
                raise DockerBackendFault("docker_authority_required")
            if self._confirmed is not None:
                return ExecutionStopRequest(
                    **self.scope.model_dump(),
                    request_id=self._confirmed.request_id,
                    reason=self._confirmed.reason,
                    manifest_digest=self.authority.manifest_digest,
                )
            return self._request

    def confirm(self, record: ExecutionStopRecord) -> None:
        with self._lock:
            if (
                self._request is None
                or record.scope != self.scope
                or (record.manifest_digest != self.authority.manifest_digest)
            ):
                raise DockerBackendFault("docker_authority_required")
            self._confirmed = record  # Canonical first SQL facts; deadlines do not move.

    @property
    def confirmed(self) -> ExecutionStopRecord | None:
        with self._lock:
            return self._confirmed

    def cap_cleanup(self, deadline: float) -> None:
        with self._lock:
            self._cleanup = min(self._cleanup, deadline)
            self._grace = min(self._grace, self._cleanup)
            self._recording = min(self._recording, deadline + 5)

    @property
    def grace_deadline(self) -> float:
        with self._lock:
            return self._grace

    @property
    def cleanup_deadline(self) -> float:
        with self._lock:
            return self._cleanup

    @property
    def recording_deadline(self) -> float:
        with self._lock:
            return self._recording


@runtime_checkable
class CancellableDockerVerifier(Protocol):
    def verify_target_cancellable(
        self, declaration: DockerTargetDeclaration, stop: ExecutionStopSignal, *, deadline: float
    ) -> VerifiedDockerTarget: ...


@runtime_checkable
class CancellableDockerBackend(Protocol):
    def attach_cancellable(
        self,
        endpoint: str,
        container_id: str,
        sink: DockerOutputSink,
        stop: ExecutionStopSignal,
        observe_running: Callable[[], None],
        *,
        deadline: float,
    ) -> int: ...
    def stop_gracefully(
        self, endpoint: str, container_id: str, *, grace_seconds: int, deadline: float
    ) -> None: ...


@runtime_checkable
class CancellableDockerController(Protocol):
    def create(
        self,
        claim: AuthorityResult,
        declaration: DockerTargetDeclaration,
        *,
        deadline: float,
        cleanup_deadline: float,
        stop: ExecutionStopSignal | None = None,
    ) -> DockerOwnedContainer: ...
    def run(
        self,
        handle: DockerOwnedContainer,
        sink: DockerOutputSink,
        *,
        deadline: float,
        cleanup_deadline: float,
        stop: ExecutionStopSignal | None = None,
    ) -> DockerJobOutcome: ...
    def finish_stop_owned(
        self,
        handle: DockerOwnedContainer,
        capture: DockerOutputCapture,
        stop: ExecutionStopSignal,
        store: "ExecutionStopStore",
    ) -> ExecutionStopRecord: ...


@runtime_checkable
class ExecutionCancellationStore(Protocol):
    def submit_cancel(
        self, call: ExecutionCancelCall, *, deadline: float
    ) -> Future[ExecutionStopRecord]: ...


@runtime_checkable
class FreshExecutionControl(Protocol):
    @property
    def shutdown_confirmed(self) -> bool: ...
    @property
    def cancellation_available(self) -> bool: ...
    def submit_cancel(
        self, call: ExecutionCancelCall, *, deadline: float
    ) -> Future[ExecutionStopRecord]: ...
    def begin_shutdown(self, deadline: float) -> None: ...
    def execution_phase(self, approval_id: UUID) -> ExecutionLivePhase | None: ...


class ExecutionStopStore(Protocol):
    def request_stop(
        self, request: ExecutionStopRequest, *, deadline: float
    ) -> ExecutionStopRecord: ...

    def inspect_stop(
        self, scope: ExecutionInspectionScope, *, deadline: float
    ) -> ExecutionStopRecord | None: ...

    def finish_stop(
        self, observation: ExecutionStopObservation, *, deadline: float
    ) -> ExecutionStopRecord: ...

    def finish_stop_with_capture(
        self,
        observation: ExecutionStopObservation,
        capture: DockerOutputSnapshot,
        *,
        deadline: float,
    ) -> ExecutionStopRecord: ...
