"""The configuration registered for one GitHub owner, read from `JobContext.connector_configuration`.

The models are strict, frozen, and reject unknown fields, so a typo in the CDK configuration fails the job.
"""

import hashlib
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

DOCUMENT_FORMAT_VERSION = "6"
"""Raise when the documents a repository produces change, so the next job reads every repository again."""


class GitHubAccessConfiguration(BaseModel):
    """How the connector paces and retries its GitHub requests."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_interval_seconds: float = Field(ge=0)
    """Delay between consecutive GitHub API requests."""
    request_timeout_seconds: int = Field(gt=0)
    retry_attempts: int = Field(ge=0)
    """Retries for a server error, a rate-limited request, or a broken archive download before the job fails."""
    retry_backoff_seconds: float = Field(ge=0)
    """The exponential backoff factor between retries of a server error or a broken archive download."""
    max_rate_limit_wait_seconds: float = Field(gt=0)
    """The longest the connector waits for a rate limit to reset before the job fails."""


GITHUB_LOGIN_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
"""A GitHub user or organization login."""
GITHUB_REPOSITORY_NAME_PATTERN = r"^[A-Za-z0-9._-]{1,100}$"
"""A repository name without its owner."""


class GitHubConnectorConfiguration(BaseModel):
    """Which repositories one owner contributes, which of their files, and how often every repository is read."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    owner: str = Field(pattern=GITHUB_LOGIN_PATTERN)
    """A GitHub login, so it names exactly one user or organization."""
    repositories: tuple[Annotated[str, Field(pattern=GITHUB_REPOSITORY_NAME_PATTERN)], ...]
    """The owner's repositories to read, by name. Empty reads the owner's most recently pushed repositories."""
    max_repositories: int = Field(gt=0)
    """How many of the owner's most recently pushed repositories are read. This bounds the index size."""
    max_repository_age_days: int = Field(gt=0)
    """A repository with no push in this many days isn't read, and its collection is deleted."""
    excluded_repository_patterns: tuple[str, ...]
    """fnmatch patterns on the repository name. A matching repository is never read."""
    excluded_path_patterns: tuple[str, ...]
    """gitignore-style patterns on the file path. A matching file is never stored."""
    max_file_bytes: int = Field(gt=0)
    """A larger file is never stored."""
    full_read_interval_days: int = Field(gt=0)
    """Every repository is read again this often, even without a new push."""
    github: GitHubAccessConfiguration

    def content_filters_hash(self) -> str:
        """A hash of every setting that changes the documents a repository produces."""
        settings = "\0".join((DOCUMENT_FORMAT_VERSION, str(self.max_file_bytes), *self.excluded_path_patterns))
        return hashlib.sha256(settings.encode()).hexdigest()
