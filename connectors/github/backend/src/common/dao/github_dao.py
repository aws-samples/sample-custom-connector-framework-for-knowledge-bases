"""List and look up GitHub repositories through PyGithub, and stream repository archives.

PyGithub paces API requests (`seconds_between_requests`), fetches each page of a listing as it
is read, and its `GithubRetry` waits out primary and secondary rate limits and retries server
errors; `github_connector` sets both from the configuration. The client is lazy, so looking up a
repository by name to read its archive link costs no request of its own. Archives come from the
URL GitHub redirects the tarball endpoint to and are read straight from the HTTP response, so a
repository never touches disk.

The Protocols below are the DAO's boundary: PyGithub and `requests` meet them, and so do the
test fakes, so neither needs a cast. Their members are read-only properties because PyGithub
exposes properties, and parameters whose names differ between PyGithub and the fakes are
positional-only.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from types import TracebackType
from typing import Any, BinaryIO, Literal, Protocol, cast, runtime_checkable

from github import UnknownObjectException
from pydantic import BaseModel, ConfigDict
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from common.exceptions import ArchiveDownloadBrokenError, RepositoryNotFoundError
from common.models import GitHubRepository

SEARCH_PAGE_SIZE = 100
"""The most results GitHub returns per page. `github_connector` sets PyGithub's `per_page` to it."""
_ARCHIVE_FORMAT = "tarball"
_BRANCH_REF_PREFIX = "refs/heads/"
_NOT_FOUND = 404


class RepositoryListing(BaseModel):
    """`GET /users/{owner}/repos` over the repositories the owner owns, most recently pushed first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["owner"] = "owner"
    sort: Literal["pushed"] = "pushed"
    direction: Literal["desc"] = "desc"


class RepositoryOwner(Protocol):
    @property
    def login(self) -> str: ...


class RepositoryRecord(Protocol):
    """PyGithub's `Repository`, as far as the DAO reads it."""

    @property
    def id(self) -> int: ...
    @property
    def full_name(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def owner(self) -> RepositoryOwner: ...
    @property
    def html_url(self) -> str: ...
    @property
    def default_branch(self) -> str: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def pushed_at(self) -> datetime: ...
    def get_archive_link(self, archive_format: str, /, ref: str) -> str: ...


class RepositoryAccount(Protocol):
    """PyGithub's `NamedUser`, a user or organization, as far as the DAO reads it."""

    def get_repos(self, *, type: str, sort: str, direction: str) -> Iterable[RepositoryRecord]: ...


@runtime_checkable
class GitHubApi(Protocol):
    """PyGithub's `Github`; runtime-checkable, so a Pydantic model can hold one."""

    def get_user(self, login: str, /) -> RepositoryAccount: ...
    def get_repo(self, full_name: str, /) -> RepositoryRecord: ...


class ArchiveResponse(Protocol):
    """A `requests` response to an archive download."""

    @property
    def status_code(self) -> int: ...
    @property
    def raw(self) -> Any:
        """urllib3's response stream; `types-requests` types it `Any`, so this boundary does too."""

    def raise_for_status(self) -> None: ...
    def __enter__(self) -> ArchiveResponse: ...
    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None: ...


@runtime_checkable
class ArchiveSession(Protocol):
    """A `requests` session; runtime-checkable, so a Pydantic model can hold one."""

    def get(self, url: str, *, stream: bool, timeout: int) -> ArchiveResponse: ...


class GitHubDao:
    """Lists public repositories and streams their default-branch archives."""

    def __init__(self, github: GitHubApi, archive_session: ArchiveSession, request_timeout_seconds: int) -> None:
        self._github = github
        self._archive_session = archive_session
        self._request_timeout_seconds = request_timeout_seconds

    def list_repositories(self, owner: str) -> Iterator[GitHubRepository]:
        """Yield every public repository the owner has, most recently pushed first; PyGithub fetches each page."""
        repositories = self._github.get_user(owner).get_repos(**RepositoryListing().model_dump())
        for repository in repositories:
            yield self._to_model(repository)

    def get_repository(self, full_name: str) -> GitHubRepository | None:
        """Return the repository named `owner/name`, or None when GitHub has no repository of that name."""
        try:
            return self._to_model(self._github.get_repo(full_name))
        except UnknownObjectException:
            return None

    @contextmanager
    def open_archive(self, repository: GitHubRepository) -> Iterator[BinaryIO]:
        """Yield the default branch's gzipped tarball as a stream read from the network.

        Raises `RepositoryNotFoundError` when GitHub no longer has the repository, and
        `ArchiveDownloadBrokenError` when the connection breaks while the caller reads.
        """
        missing = RepositoryNotFoundError(f"GitHub has no archive of {repository.full_name}.")
        try:
            # The full ref, because GitHub answers 300 instead of a redirect when a tag shares the branch's name.
            url = self._github.get_repo(repository.full_name).get_archive_link(
                _ARCHIVE_FORMAT, ref=f"{_BRANCH_REF_PREFIX}{repository.default_branch}"
            )
        except UnknownObjectException as error:
            raise missing from error
        with self._archive_session.get(url, stream=True, timeout=self._request_timeout_seconds) as response:
            if response.status_code == _NOT_FOUND:
                raise missing
            response.raise_for_status()
            # Undo any HTTP content encoding, so the stream is the archive file itself.
            response.raw.decode_content = True
            try:
                yield cast(BinaryIO, response.raw)
            except (ProtocolError, ReadTimeoutError) as error:
                raise ArchiveDownloadBrokenError(
                    f"The archive download of {repository.full_name} broke off."
                ) from error

    @staticmethod
    def _to_model(repository: RepositoryRecord) -> GitHubRepository:
        return GitHubRepository(
            id=repository.id,
            full_name=repository.full_name,
            name=repository.name,
            owner=repository.owner.login,
            html_url=repository.html_url,
            default_branch=repository.default_branch,
            created_at=repository.created_at,
            pushed_at=repository.pushed_at,
        )
