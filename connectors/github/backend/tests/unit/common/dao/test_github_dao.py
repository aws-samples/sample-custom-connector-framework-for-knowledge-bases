"""How the GitHub DAO maps PyGithub results and errors to the connector's models."""

from datetime import UTC, datetime

import pytest

from common.exceptions import ArchiveDownloadBrokenError, ArchiveDownloadRefusedError, RepositoryNotFoundError
from common.models import GitHubRepository
from tests.unit.conftest import make_github_dao
from tests.unit.fake_github import FakeGithub, FakeRepository


def _repository(fake: FakeRepository) -> GitHubRepository:
    return GitHubRepository(
        id=fake.id,
        full_name=fake.full_name,
        name=fake.name,
        owner=fake.owner,
        html_url=f"https://github.com/{fake.full_name}",
        default_branch=fake.default_branch,
        created_at=datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC),
        pushed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_the_listing_yields_the_owners_repositories_most_recently_pushed_first() -> None:
    repositories = [
        FakeRepository("aws-samples/older", {}, pushed_at="2026-01-01T00:00:00Z"),
        FakeRepository("aws-samples/newer", {}, pushed_at="2026-02-01T00:00:00Z"),
        FakeRepository("awslabs/other-owner", {}, pushed_at="2026-03-01T00:00:00Z"),
    ]

    listed = make_github_dao(FakeGithub(repositories)).list_repositories("aws-samples")

    assert [repository.full_name for repository in listed] == ["aws-samples/newer", "aws-samples/older"]


def test_a_listed_repository_keeps_its_github_id_owner_default_branch_and_last_push() -> None:
    fake = FakeRepository("aws-samples/demo", {}, default_branch="mainline", github_id=42)

    found = tuple(make_github_dao(FakeGithub([fake])).list_repositories("aws-samples"))

    assert found == (_repository(fake),)


def test_the_default_branch_archive_is_streamed_without_touching_disk() -> None:
    fake = FakeRepository("aws-samples/demo", {"README.md": b"# Demo\n"}, default_branch="mainline")

    with make_github_dao(FakeGithub([fake])).open_archive(_repository(fake)) as archive:
        assert archive.read() == fake.tarball()


def test_the_default_branch_archive_is_read_when_a_tag_has_the_branchs_name() -> None:
    fake = FakeRepository(
        "aws-samples/demo", {"README.md": b"# Demo\n"}, default_branch="v1.0.0", tags=frozenset({"v1.0.0"})
    )

    with make_github_dao(FakeGithub([fake])).open_archive(_repository(fake)) as archive:
        assert archive.read() == fake.tarball()


def test_a_repository_github_no_longer_has_raises_repository_not_found() -> None:
    fake = FakeRepository("aws-samples/demo", {})

    with pytest.raises(RepositoryNotFoundError):
        with make_github_dao(FakeGithub([])).open_archive(_repository(fake)):
            pass


def test_an_archive_github_answers_with_404_raises_repository_not_found() -> None:
    fake = FakeRepository("aws-samples/demo", {}, archive_missing=True)

    with pytest.raises(RepositoryNotFoundError):
        with make_github_dao(FakeGithub([fake])).open_archive(_repository(fake)):
            pass


@pytest.mark.parametrize("status", [300, 403, 500])
def test_an_archive_github_answers_with_another_status_raises_archive_download_refused(status: int) -> None:
    fake = FakeRepository("aws-samples/demo", {"README.md": b"# Demo\n"}, archive_status=status)

    with pytest.raises(ArchiveDownloadRefusedError, match=f"aws-samples/demo with HTTP {status}"):
        with make_github_dao(FakeGithub([fake])).open_archive(_repository(fake)):
            pass


def test_a_download_that_breaks_off_while_read_raises_archive_download_broken() -> None:
    fake = FakeRepository("aws-samples/demo", {"README.md": b"# Demo\n"}, broken_downloads=1)

    with pytest.raises(ArchiveDownloadBrokenError):
        with make_github_dao(FakeGithub([fake])).open_archive(_repository(fake)) as archive:
            archive.read()


def test_get_repository_returns_the_repository_by_name() -> None:
    fake = FakeRepository("awslabs/git-secrets", {})

    assert make_github_dao(FakeGithub([fake])).get_repository("awslabs/git-secrets") == _repository(fake)


def test_get_repository_returns_none_when_github_has_no_repository_of_that_name() -> None:
    assert make_github_dao(FakeGithub([])).get_repository("awslabs/gone") is None
