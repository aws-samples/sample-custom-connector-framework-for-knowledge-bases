"""Choose one owner's repositories for a run.

When the configuration lists repositories, those are read by name, and a name GitHub no longer
has is dropped, so its collection is deleted. The age limit and the name patterns don't apply to
a listed repository. Otherwise the owner's repositories are listed most recently pushed first,
up to `max_repositories`, and the listing stops at the first one older than the age limit, so a
repository that ages out is no longer selected and its collection is deleted.
"""

import logging
from collections.abc import Iterator
from datetime import timedelta
from fnmatch import fnmatchcase

from pydantic import BaseModel, ConfigDict

from common.clock import Clock
from common.configuration import GitHubConnectorConfiguration
from common.dao.github_dao import GitHubDao
from common.models import GitHubRepository

logger = logging.getLogger(__name__)


class SelectRepositoriesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    repositories: tuple[GitHubRepository, ...]


class RepositorySelector:
    """Reads the listed repositories, or lists the owner's repositories up to `max_repositories`."""

    def __init__(self, github: GitHubDao, clock: Clock) -> None:
        self._github = github
        self._clock = clock

    def select(self, configuration: GitHubConnectorConfiguration) -> SelectRepositoriesResponse:
        found = self._listed(configuration) if configuration.repositories else self._owned(configuration)
        repositories = tuple(found)
        logger.info("repositories_selected", extra={"owner": configuration.owner, "repositories": len(repositories)})
        return SelectRepositoriesResponse(repositories=repositories)

    def _listed(self, configuration: GitHubConnectorConfiguration) -> Iterator[GitHubRepository]:
        for name in configuration.repositories:
            full_name = f"{configuration.owner}/{name}"
            repository = self._github.get_repository(full_name)
            if repository is None:
                logger.warning("repository_dropped", extra={"repository": full_name, "reason": "not_found"})
                continue
            yield repository

    def _owned(self, configuration: GitHubConnectorConfiguration) -> Iterator[GitHubRepository]:
        cutoff = self._clock.now() - timedelta(days=configuration.max_repository_age_days)
        excluded_patterns = tuple(pattern.lower() for pattern in configuration.excluded_repository_patterns)
        found = 0
        for repository in self._github.list_repositories(configuration.owner):
            # Listed most recently pushed first, so the first one older than the cutoff ends the selection.
            if repository.pushed_at < cutoff or found == configuration.max_repositories:
                return
            if any(fnmatchcase(repository.name.lower(), pattern) for pattern in excluded_patterns):
                logger.info("repository_excluded", extra={"repository": repository.full_name})
                continue
            yield repository
            found += 1
