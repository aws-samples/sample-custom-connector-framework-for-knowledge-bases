"""The repository fields the connector reads, and the checkpoints it saves between jobs."""

import hashlib

from pydantic import AwareDatetime, BaseModel, ConfigDict

_REPOSITORY_ID_PREFIX = "repo_"
_REPOSITORY_ID_HEX_CHARS = 16


class GitHubRepository(BaseModel):
    """One repository as the connector needs it, independent of the GitHub library that fetched it."""

    model_config = ConfigDict(frozen=True)

    id: int
    """GitHub's numeric repository ID. It stays the same when the repository is renamed or moves to another owner."""
    full_name: str
    name: str
    owner: str
    """The login of the user or organization that owns the repository."""
    html_url: str
    default_branch: str
    created_at: AwareDatetime
    pushed_at: AwareDatetime
    """When anything was last pushed to the repository, as GitHub reports it. A new push changes it."""

    @property
    def collection_id(self) -> str:
        """The repository's numeric ID, which names its collection in the store."""
        return str(self.id)

    @property
    def repository_id_attribute(self) -> str:
        """`repo_` plus a hash of the numeric ID: the `repository_id` attribute value search filters on."""
        digest = hashlib.sha256(f"github:{self.id}".encode()).hexdigest()[:_REPOSITORY_ID_HEX_CHARS]
        return f"{_REPOSITORY_ID_PREFIX}{digest}"


class SourceCheckpoint(BaseModel):
    """The default collection's checkpoint: when every selected repository was last read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    last_full_read_at: AwareDatetime


class RepositoryCheckpoint(BaseModel):
    """A repository collection's checkpoint: the push and content filters its stored documents came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pushed_at: AwareDatetime
    filters: str
