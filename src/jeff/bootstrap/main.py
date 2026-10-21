"""Wire project ports to local adapters without starting a daemon."""

from jeff.application.projects import ProjectService
from jeff.application.questions import QuestionService
from jeff.infrastructure.project_files import FileProjectStore
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead
from jeff.interfaces.cli import create_app


def main() -> None:
    """Construct adapters; actual I/O occurs only when a project command runs."""
    service = ProjectService(GitProjectDiscovery(), FileProjectStore())
    questions = QuestionService(service, GitRepositoryRead())
    create_app(service, questions)()
