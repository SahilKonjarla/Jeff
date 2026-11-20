"""Explicit metadata/lifecycle transport exports; no routing or launch policy."""

# Keep this shared module export for existing no-child/fake-process test hooks.
import subprocess as subprocess

from .diagnostics import _failure_facts as _failure_facts
from .lifecycle import DockerLifecycleCLI
from .metadata import _Engine as _Engine
from .metadata import _Image as _Image
from .metadata import _ImageConfig as _ImageConfig
from .metadata import _json as _json
from .metadata import _Metadata as _Metadata
from .queries import _ENGINE_FORMAT as _ENGINE_FORMAT
from .queries import _IMAGE_FORMAT as _IMAGE_FORMAT
from .queries import _LIMIT as _LIMIT
from .queries import Query
from .queries import _Reader as _Reader
from .transport import _construction_budget as _construction_budget
from .transport import _DockerCLI as _DockerCLI
from .transport import _safe_ancestors as _safe_ancestors
from .transport import _signature as _signature
from .verification import DockerTargetInspector

__all__ = [
    "DockerLifecycleCLI",
    "DockerTargetInspector",
    "Query",
    "subprocess",
]
