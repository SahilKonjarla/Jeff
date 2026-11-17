"""Read-only diagnostics: authenticated live caches or leased stopped-state inspection."""

import os as os
import re as re
import subprocess as subprocess
import sys as sys
import time as time
from collections.abc import Callable as Callable
from typing import Literal as Literal
from typing import cast as cast

from pydantic import ValidationError as ValidationError

from jeff.application.diagnostics import DOCTOR_CHECKS as DOCTOR_CHECKS
from jeff.application.diagnostics import DiagnosticDeadlineExceeded as DiagnosticDeadlineExceeded
from jeff.application.diagnostics import DoctorCheck as DoctorCheck
from jeff.application.diagnostics import DoctorReason as DoctorReason
from jeff.application.diagnostics import DoctorReport as DoctorReport
from jeff.application.diagnostics import DoctorResult as DoctorResult
from jeff.application.diagnostics import DoctorRuntimeData as DoctorRuntimeData
from jeff.application.diagnostics import doctor_result as doctor_result
from jeff.application.projects import Failure as Failure
from jeff.application.runtime import ControlFault as ControlFault
from jeff.application.runtime import RuntimeLayout as RuntimeLayout
from jeff.application.runtime import RuntimePathInspection as RuntimePathInspection
from jeff.application.storage import DATABASE_SCHEMA as DATABASE_SCHEMA
from jeff.application.storage import LoadedRuntimeSettings as LoadedRuntimeSettings
from jeff.application.storage import StorageFault as StorageFault
from jeff.domain.runtime import RuntimeState as RuntimeState
from jeff.infrastructure.runtime_ipc import inspect_control as inspect_control
from jeff.infrastructure.runtime_paths import inspect_layout as inspect_layout
from jeff.infrastructure.runtime_settings import read_existing_settings as read_existing_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore as RuntimeStateStore
from jeff.infrastructure.runtime_storage import inspect_stopped_storage as inspect_stopped_storage

from .adapter import RuntimeDiagnosticsAdapter as RuntimeDiagnosticsAdapter
from .reasons import _PATH_REASONS as _PATH_REASONS
