"""In-memory stand-ins for PyGithub's `Github` and the archive download session.

The fakes answer only the calls `GitHubDao` makes, the way PyGithub and GitHub do: an owner's
repositories most recently pushed first, `UnknownObjectException` for a missing repository, and
an archive URL for the default branch that the download session streams.
"""

import io
import tarfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any

from github import GithubException, UnknownObjectException
from urllib3.exceptions import ProtocolError

from common.dao.github_dao import RepositoryListing

_ARCHIVE_HOST = "https://codeload.example.test"
_NOT_FOUND = 404
_FORBIDDEN = 403
_SERVER_ERROR = 500
_OK = 200
_ARCHIVE_ROOT_SHA = "1111111"


@dataclass
class FakeRepository:
    full_name: str
    files: dict[str, bytes]
    pushed_at: str = "2026-10-01T00:00:00Z"
    created_at: str = "2024-05-06T07:08:09Z"
    default_branch: str = "main"
    symlinks: dict[str, str] = field(default_factory=dict)
    archive_missing: bool = False
    """GitHub returns 404 for the archive, as it does when a repository is deleted after the search."""
    github_id: int | None = None
    """GitHub's numeric ID. Defaults to a value derived from the original name, so a rename can keep it."""
    broken_downloads: int = 0
    """How many of the next archive downloads break off halfway, the way a dropped connection does."""
    tags: frozenset[str] = frozenset()
    """Tag names. GitHub answers 300, not a redirect, to an archive of a short ref that is also a tag."""

    @property
    def owner(self) -> str:
        return self.full_name.split("/")[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/")[1]

    @property
    def id(self) -> int:
        return self.github_id if self.github_id is not None else zlib.crc32(self.full_name.lower().encode())

    def tarball(self) -> bytes:
        root = f"{self.owner}-{self.name}-{_ARCHIVE_ROOT_SHA}"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path, content in self.files.items():
                member = tarfile.TarInfo(f"{root}/{path}")
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            for path, target in self.symlinks.items():
                link = tarfile.TarInfo(f"{root}/{path}")
                link.type = tarfile.SYMTYPE
                link.linkname = target
                archive.addfile(link)
        return buffer.getvalue()


class _Owner:
    def __init__(self, login: str) -> None:
        self.login = login


class FakePyGithubRepository:
    """The PyGithub `Repository` attributes and methods `GitHubDao` reads."""

    def __init__(self, fake: FakeRepository, github: FakeGithub) -> None:
        self._fake = fake
        self._github = github
        self.id = fake.id
        self.full_name = fake.full_name
        self.name = fake.name
        self.owner = _Owner(fake.owner)
        self.html_url = f"https://github.com/{fake.full_name}"
        self.default_branch = fake.default_branch
        self.pushed_at = datetime.fromisoformat(fake.pushed_at)
        self.created_at = datetime.fromisoformat(fake.created_at)

    def get_archive_link(self, archive_format: str, ref: str) -> str:
        self._github.check_access(self._fake.full_name)
        assert archive_format == "tarball"
        if ref in self._fake.tags:
            # PyGithub reads the redirect's Location header, which GitHub's 300 answer doesn't have.
            raise KeyError("location")
        return f"{_ARCHIVE_HOST}/{self._fake.full_name}/tar.gz/{ref}"


class _Account:
    """A user or organization whose repositories the fake lists, most recently pushed first."""

    def __init__(self, login: str, github: FakeGithub) -> None:
        self._login = login
        self._github = github

    def get_repos(self, **listing: str) -> list[FakePyGithubRepository]:
        RepositoryListing.model_validate(listing)
        github = self._github
        if github.server_error:
            raise GithubException(_SERVER_ERROR)
        owned = [repository for repository in github.repositories.values() if repository.owner == self._login]
        owned.sort(key=lambda repository: repository.pushed_at, reverse=True)
        return [FakePyGithubRepository(repository, github) for repository in owned]


class FakeGithub:
    """Implements repository listings and repository lookups over a set of fake repositories."""

    def __init__(self, repositories: list[FakeRepository]) -> None:
        self.repositories = {repository.full_name.lower(): repository for repository in repositories}
        self.forbidden_repository: str | None = None
        self.server_error = False
        """Every listing answers 500, as GitHub does when retries are used up on a server error."""
        self.downloads: list[str] = []
        """The full name of every repository whose archive was served, in order."""

    def check_access(self, full_name: str) -> None:
        if self.forbidden_repository is not None and full_name.lower() == self.forbidden_repository.lower():
            raise GithubException(_FORBIDDEN)

    def get_user(self, login: str) -> _Account:
        return _Account(login, self)

    def get_repo(self, full_name: str) -> FakePyGithubRepository:
        self.check_access(full_name)
        repository = self.repositories.get(full_name.lower())
        if repository is None:
            raise UnknownObjectException(_NOT_FOUND)
        return FakePyGithubRepository(repository, self)


class _ChunkedStream(io.RawIOBase):
    """A network stream that returns at most `chunk_bytes` per read, the way a socket can.

    A broken stream raises urllib3's `ProtocolError` once half the body is read, as a dropped
    connection does.
    """

    def __init__(self, body: bytes, chunk_bytes: int, broken: bool = False) -> None:
        self._body = io.BytesIO(body)
        self._chunk_bytes = chunk_bytes
        self._break_at = len(body) // 2 if broken else None
        self.decode_content = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        limit = min(len(buffer), self._chunk_bytes)
        if self._break_at is not None:
            if self._body.tell() >= self._break_at:
                raise ProtocolError("Connection broken: IncompleteRead(0 bytes read)")
            limit = min(limit, self._break_at - self._body.tell())
        chunk = self._body.read(limit)
        buffer[: len(chunk)] = chunk
        return len(chunk)


class _ArchiveResponse:
    def __init__(self, status_code: int, body: bytes, chunk_bytes: int, broken: bool = False) -> None:
        self.status_code = status_code
        self.raw = _ChunkedStream(body, chunk_bytes, broken)

    def __enter__(self) -> _ArchiveResponse:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        return None

    def raise_for_status(self) -> None:
        assert self.status_code == _OK


class FakeArchiveSession:
    """Serves the default branch's tarball an archive URL names, or 404, in small chunks."""

    def __init__(self, github: FakeGithub, chunk_bytes: int = 4096) -> None:
        self._github = github
        self._chunk_bytes = chunk_bytes

    def get(self, url: str, stream: bool, timeout: int) -> _ArchiveResponse:
        assert stream
        owner, name, _, ref = url.removeprefix(f"{_ARCHIVE_HOST}/").split("/", 3)
        repository = self._github.repositories.get(f"{owner}/{name}".lower())
        if (
            repository is None
            or repository.archive_missing
            or ref
            not in {
                repository.default_branch,
                f"refs/heads/{repository.default_branch}",
            }
        ):
            return _ArchiveResponse(_NOT_FOUND, b"", self._chunk_bytes)
        self._github.downloads.append(repository.full_name)
        broken = repository.broken_downloads > 0
        repository.broken_downloads -= int(broken)
        return _ArchiveResponse(_OK, repository.tarball(), self._chunk_bytes, broken)
