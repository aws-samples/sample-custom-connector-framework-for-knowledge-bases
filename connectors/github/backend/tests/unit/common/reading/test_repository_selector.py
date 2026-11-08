"""Which repositories one owner contributes."""

from dataclasses import dataclass

from common.reading.repository_selector import RepositorySelector
from tests.configurations import make_configuration
from tests.unit.conftest import FixedClock, make_github_dao
from tests.unit.fake_github import FakeGithub, FakeRepository


@dataclass(frozen=True)
class _Selection:
    repositories: list[FakeRepository]
    max_repositories: int = 2


def _select(selection: _Selection) -> list[str]:
    selector = RepositorySelector(github=make_github_dao(FakeGithub(selection.repositories)), clock=FixedClock())
    configuration = make_configuration().model_copy(update={"max_repositories": selection.max_repositories})
    response = selector.select(configuration)
    return [repository.full_name for repository in response.repositories]


def test_the_most_recently_updated_repositories_are_chosen_up_to_the_limit() -> None:
    repositories = [
        FakeRepository(f"aws-samples/repo-{day}", {}, pushed_at=f"2026-10-{day:02d}T00:00:00Z") for day in (1, 9, 5)
    ]

    assert _select(_Selection(repositories, max_repositories=2)) == ["aws-samples/repo-9", "aws-samples/repo-5"]


def test_the_listing_continues_past_githubs_1000_result_search_limit() -> None:
    repositories = [
        FakeRepository(f"aws-samples/repo-{index}", {}, pushed_at=f"2026-10-{1 + index % 28:02d}T00:00:00Z")
        for index in range(1_200)
    ]

    assert len(_select(_Selection(repositories, max_repositories=1_100))) == 1_100


def test_repositories_matching_an_excluded_pattern_are_never_chosen() -> None:
    repositories = [
        FakeRepository("aws-samples/AWS-SDK-python", {}, pushed_at="2026-10-09T00:00:00Z"),
        FakeRepository("aws-samples/demo-sdk", {}, pushed_at="2026-10-08T00:00:00Z"),
        FakeRepository("aws-samples/app", {}),
    ]

    assert _select(_Selection(repositories)) == ["aws-samples/app"]


def test_repositories_older_than_the_age_limit_are_not_chosen() -> None:
    repositories = [
        FakeRepository("aws-samples/fresh", {}),
        FakeRepository("aws-samples/old", {}, pushed_at="2024-01-01T00:00:00Z"),
    ]

    assert _select(_Selection(repositories, max_repositories=5)) == ["aws-samples/fresh"]


def _select_listed(repositories: list[FakeRepository], listed: tuple[str, ...]) -> list[str]:
    selector = RepositorySelector(github=make_github_dao(FakeGithub(repositories)), clock=FixedClock())
    response = selector.select(make_configuration().model_copy(update={"repositories": listed, "max_repositories": 1}))
    return [repository.full_name for repository in response.repositories]


def test_listed_repositories_are_read_by_name_and_no_other_repository_is_chosen() -> None:
    repositories = [
        FakeRepository("aws-samples/listed-a", {}),
        FakeRepository("aws-samples/listed-b", {}),
        FakeRepository("aws-samples/not-listed", {}, pushed_at="2026-10-29T00:00:00Z"),
    ]

    assert _select_listed(repositories, ("listed-a", "listed-b")) == ["aws-samples/listed-a", "aws-samples/listed-b"]


def test_a_listed_repository_is_read_whatever_its_age_or_name_pattern() -> None:
    repositories = [
        FakeRepository("aws-samples/aws-sdk-old", {}, pushed_at="2020-01-01T00:00:00Z"),
    ]

    assert _select_listed(repositories, ("aws-sdk-old",)) == ["aws-samples/aws-sdk-old"]


def test_a_listed_repository_github_no_longer_has_is_dropped() -> None:
    repositories = [FakeRepository("aws-samples/kept", {})]

    assert _select_listed(repositories, ("gone", "kept")) == ["aws-samples/kept"]
