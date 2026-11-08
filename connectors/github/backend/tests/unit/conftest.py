"""Shared builders for the GitHub connector's tests."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar

from ccf_api_models.job_container import JobExitCode
from custom_connector_framework import DocumentStore
from custom_connector_framework.testing import (
    RunImplementationRequest,
    SeedCollection,
    SeedDocument,
    SeedState,
    TestRun,
    run_implementation,
)

from common.clock import Clock
from common.configuration import GitHubConnectorConfiguration
from common.dao.github_dao import GitHubDao
from common.sleeper import Sleeper
from github_connector import ClientsFactory, GitHubClients, GitHubClientsRequest, GitHubConnectorImplementation
from tests.unit.fake_github import FakeArchiveSession, FakeGithub

FIXED_NOW = datetime(2026, 10, 30, tzinfo=UTC)
TOKEN = "ghp_" + "1" * 36


def failure_trace(run: TestRun) -> str:
    """The stack trace a run that ended 70 logged; the runner logs the implementation's error and exits."""
    assert run.exit_code is JobExitCode.CONNECTOR_IMPLEMENTATION_ERROR
    [failed] = [event for event in run.log_events if event.message == "connector_implementation_failed"]
    assert failed.stack_trace is not None
    return failed.stack_trace


def make_github_dao(github: FakeGithub) -> GitHubDao:
    return GitHubDao(github=github, archive_session=FakeArchiveSession(github), request_timeout_seconds=5)


class FixedClock:
    """A clock that returns the time a test sets."""

    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class RecordingSleeper:
    """Records each ceiling the connector would sleep up to, and doesn't sleep."""

    def __init__(self) -> None:
        self.ceilings: list[float] = []

    def sleep_up_to(self, ceiling_seconds: float) -> None:
        self.ceilings.append(ceiling_seconds)


class FakeClientsFactory:
    """Returns the fake GitHub, and records the token the connector built its client with."""

    def __init__(self, github: FakeGithub) -> None:
        self._github = github
        self.tokens: list[str] = []

    def create(self, request: GitHubClientsRequest) -> GitHubClients:
        self.tokens.append(request.token.get_secret_value())
        return GitHubClients(github=self._github, archive_session=FakeArchiveSession(self._github))


@dataclass
class ConnectorRuns:
    """Runs the GitHub connector against a fake GitHub, carrying the stored records from one run to the next."""

    github: FakeGithub
    clock: FixedClock = field(default_factory=FixedClock)
    sleeper: RecordingSleeper = field(default_factory=RecordingSleeper)
    seed: SeedState = field(default_factory=SeedState)
    stores: list[DocumentStore] = field(default_factory=list)
    """The store of every run, so a test can read the report of a run that raised."""

    def __post_init__(self) -> None:
        self.clients = FakeClientsFactory(self.github)

    def run(self, configuration: GitHubConnectorConfiguration) -> TestRun:
        result = run_implementation(
            RunImplementationRequest(
                implementation=self.implementation(),
                connector_configuration=configuration.model_dump_json(),
                secret=TOKEN,
                seed=self.seed,
            )
        )
        self.seed = SeedState(
            documents=tuple(
                SeedDocument(
                    document_id=document.document_id,
                    collection_id=document.collection_id,
                    source_version=document.source_version,
                    content_hash=document.content_hash,
                )
                for document in result.documents
            ),
            collections=tuple(
                SeedCollection(collection_id=collection.collection_id, checkpoint=collection.checkpoint)
                for collection in result.collections
            ),
        )
        return result

    def implementation(self) -> type[GitHubConnectorImplementation]:
        """The connector, wired to the fake GitHub and this run's clock."""
        runs = self

        class FakeGitHubImplementation(GitHubConnectorImplementation):
            clients_factory: ClassVar[ClientsFactory] = runs.clients
            clock: ClassVar[Clock] = runs.clock
            sleeper: ClassVar[Sleeper] = runs.sleeper

            def run(self) -> None:
                runs.stores.append(self.store)
                super().run()

        return FakeGitHubImplementation
