"""The GitHub connector: reads the recently updated public repositories of one GitHub owner into a destination.

The image's entry point is `ccf-connector run`, and the custom connector's command adds
`github_connector:GitHubConnectorImplementation`. `run()` is the composition root: it parses the
configuration, builds the GitHub clients with the token from `JobContext.secret`, and reads the
owner's selected repositories.
"""

import random
import time
from typing import ClassVar, Protocol

import requests
from custom_connector_framework import ConnectorImplementation
from github import Auth, Github, GithubRetry
from pathspec import GitIgnoreSpec
from pydantic import BaseModel, ConfigDict, SecretStr
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from activities.owner_read_activity import OwnerReadActivity
from common.clock import Clock, UtcClock
from common.configuration import GitHubAccessConfiguration, GitHubConnectorConfiguration
from common.dao.github_dao import SEARCH_PAGE_SIZE, ArchiveSession, GitHubApi, GitHubDao
from common.exceptions import MissingConnectorInputError
from common.reading.archive_reader import ArchiveReader
from common.reading.document_builder import RepositoryDocumentBuilder
from common.reading.repository_reader import RepositoryReader
from common.reading.repository_selector import RepositorySelector
from common.sleeper import JitteredSleeper, Sleeper

_SERVER_ERRORS = tuple(range(500, 600))
_HTTPS = "https://"


class GitHubClientsRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    access: GitHubAccessConfiguration
    token: SecretStr


class GitHubClients(BaseModel):
    """PyGithub for listings and lookups, and a requests session for archive downloads, typed by the DAO's Protocols."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    github: GitHubApi
    archive_session: ArchiveSession


class ClientsFactory(Protocol):
    """Builds the GitHub clients one job uses; tests give the connector one that returns fakes."""

    def create(self, request: GitHubClientsRequest) -> GitHubClients: ...


class GitHubClientsFactory:
    """Builds the GitHub clients, paced and retried as configured."""

    def create(self, request: GitHubClientsRequest) -> GitHubClients:
        access = request.access
        github = Github(
            auth=Auth.Token(request.token.get_secret_value()),
            per_page=SEARCH_PAGE_SIZE,
            lazy=True,
            timeout=access.request_timeout_seconds,
            seconds_between_requests=access.request_interval_seconds,
            retry=GithubRetry(
                total=access.retry_attempts,
                backoff_factor=access.retry_backoff_seconds,
                backoff_jitter=access.retry_backoff_seconds,
                max_rate_limit_wait=access.max_rate_limit_wait_seconds,
            ),
        )
        archive_session = requests.Session()
        archive_session.mount(
            _HTTPS,
            HTTPAdapter(
                max_retries=Retry(
                    total=access.retry_attempts,
                    backoff_factor=access.retry_backoff_seconds,
                    backoff_jitter=access.retry_backoff_seconds,
                    status_forcelist=_SERVER_ERRORS,
                    allowed_methods=("GET",),
                    raise_on_status=False,
                )
            ),
        )
        return GitHubClients(github=github, archive_session=archive_session)


class GitHubConnectorImplementation(ConnectorImplementation):
    """Reads the configured owner. The class attributes are the clients, clock, and sleeper tests replace."""

    clients_factory: ClassVar[ClientsFactory] = GitHubClientsFactory()
    clock: ClassVar[Clock] = UtcClock()
    sleeper: ClassVar[Sleeper] = JitteredSleeper(sleep=time.sleep, jitter=random.SystemRandom())

    def run(self) -> None:
        job = self.job
        if job.connector_configuration is None or job.secret is None:
            raise MissingConnectorInputError(
                "The custom connector needs a connector_configuration and a secret_arn holding a GitHub token. "
                "Register both, then start the job again."
            )
        configuration = GitHubConnectorConfiguration.model_validate_json(job.connector_configuration.get_secret_value())
        clients = self.clients_factory.create(GitHubClientsRequest(access=configuration.github, token=job.secret))
        github = GitHubDao(
            github=clients.github,
            archive_session=clients.archive_session,
            request_timeout_seconds=configuration.github.request_timeout_seconds,
        )
        activity = OwnerReadActivity(
            selector=RepositorySelector(github=github, clock=self.clock),
            repository_reader=RepositoryReader(
                github=github,
                archive_reader=ArchiveReader(GitIgnoreSpec.from_lines(configuration.excluded_path_patterns)),
                document_builder=RepositoryDocumentBuilder(),
                store=self.store,
                sleeper=self.sleeper,
            ),
            store=self.store,
            clock=self.clock,
        )
        activity.read(configuration)
