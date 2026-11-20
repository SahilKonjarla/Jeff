"""Internal control child composition; not a public CLI lifecycle command."""

import argparse as argparse
import asyncio as asyncio
import fcntl as fcntl
import os as os
import stat as stat
import sys as sys
import time as time
from pathlib import Path as Path
from typing import cast as cast
from uuid import UUID as UUID

from jeff.application.diagnostics import RuntimeDiagnostics as RuntimeDiagnostics
from jeff.application.dispatch import ActivityClient as ActivityClient
from jeff.application.dispatch import ProjectClient as ProjectClient
from jeff.application.dispatch import ProjectDispatcher as ProjectDispatcher
from jeff.application.dispatch import ProjectStatusService as ProjectStatusService
from jeff.application.execution import DockerBackendFault as DockerBackendFault
from jeff.application.execution import DockerCLIIdentity as DockerCLIIdentity
from jeff.application.execution import DockerTargetDeclaration as DockerTargetDeclaration
from jeff.application.execution import ExecutionCancelClient as ExecutionCancelClient
from jeff.application.execution import ExecutionClient as ExecutionClient
from jeff.application.execution import ExecutionService as ExecutionService
from jeff.application.execution import FreshExecutionClient as FreshExecutionClient
from jeff.application.execution import fresh_execution_failure as fresh_execution_failure
from jeff.application.execution_coordinator import ExecutionCoordinator as ExecutionCoordinator
from jeff.application.projects import Failure as Failure
from jeff.application.projects import ProjectService as ProjectService
from jeff.application.questions import QuestionService as QuestionService
from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS as DEFAULT_RUNTIME_LIMITS
from jeff.application.runtime import PROTOCOL_REVISION as PROTOCOL_REVISION
from jeff.application.runtime import ControlFault as ControlFault
from jeff.application.runtime import ControlIssueCode as ControlIssueCode
from jeff.application.runtime import RuntimeControl as RuntimeControl
from jeff.application.runtime import RuntimeIdentityData as RuntimeIdentityData
from jeff.application.runtime import RuntimeLayout as RuntimeLayout
from jeff.application.runtime import RuntimePathInputs as RuntimePathInputs
from jeff.application.runtime import StartupResult as StartupResult
from jeff.application.storage import StorageFault as StorageFault
from jeff.infrastructure.docker_cli import DockerLifecycleCLI as DockerLifecycleCLI
from jeff.infrastructure.docker_cli import DockerTargetInspector as DockerTargetInspector
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore as FilePreparedInputStore
from jeff.infrastructure.project_files import FileProjectStore as FileProjectStore
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher as RuntimeDispatcher
from jeff.infrastructure.runtime_events import RuntimeEvents as RuntimeEvents
from jeff.infrastructure.runtime_execution import DockerJobController as DockerJobController
from jeff.infrastructure.runtime_ipc import ControlServer as ControlServer
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient as RuntimeProjectClient
from jeff.infrastructure.runtime_ipc import _stream_send as _stream_send
from jeff.infrastructure.runtime_ipc import encode_message as encode_message
from jeff.infrastructure.runtime_output import GuardedOutputCollector as GuardedOutputCollector
from jeff.infrastructure.runtime_paths import resolve_layout as resolve_layout
from jeff.infrastructure.runtime_process import RuntimeController as RuntimeController
from jeff.infrastructure.runtime_state import Ownership as Ownership
from jeff.infrastructure.runtime_state import RuntimeStateStore as RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage as SQLiteStorage
from jeff.integrations.git import GitProjectDiscovery as GitProjectDiscovery
from jeff.integrations.git import GitRepositoryRead as GitRepositoryRead
from jeff.integrations.git.worker import OwnedGitRunner as OwnedGitRunner

from .child import main as main
from .clients import _selected_layout as _selected_layout
from .clients import create_activity_client as create_activity_client
from .clients import create_cancellation_client as create_cancellation_client
from .clients import create_execution_client as create_execution_client
from .clients import create_fresh_execution_client as create_fresh_execution_client
from .clients import create_project_client as create_project_client
from .clients import create_runtime_controller as create_runtime_controller
from .clients import create_runtime_diagnostics as create_runtime_diagnostics
from .execution import create_configured_execution as create_configured_execution
from .execution import create_internal_execution as create_internal_execution
from .serving import _serve as _serve
