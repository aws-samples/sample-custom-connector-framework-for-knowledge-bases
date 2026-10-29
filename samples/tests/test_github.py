"""Focused mapping tests for the GitHub connector definition."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from ccf_api_models.models import DestinationMode
from custom_connector_framework import ConnectorContext, EmptyJobConfiguration
from pydantic import ValidationError

from github_connector import connector
from github_connector.connector import GitHubConnectorConfiguration, GitHubRepositoryConfiguration
from tests.runtime_harness import RuntimeHarness


class FakeSourceSession:
    """Source session that records credential access."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.client_names: list[str] = []
        self.secret_ids: list[str] = []

    def client(self, service_name: str) -> Any:
        self.client_names.append(service_name)
        return self

    def get_secret_value(self, **kwargs: str) -> dict[str, str]:
        self.secret_ids.append(kwargs["SecretId"])
        return {"SecretString": self.token}


class FakePaginatedItems:
    """Lazy GitHub page sequence with optional partial-enumeration failure."""

    def __init__(self, entries: list[Any], fail_after: int | None = None) -> None:
        self.entries = entries
        self.fail_after = fail_after

    def __iter__(self) -> Iterator[Any]:
        for index, entry in enumerate(self.entries):
            if self.fail_after == index:
                raise RuntimeError("private GitHub enumeration failure")
            yield entry
        if self.fail_after == len(self.entries):
            raise RuntimeError("private GitHub enumeration failure")


class FakeRepository:
    """GitHub repository with an immutable API identity and nested files."""

    def __init__(
        self,
        repository_id: int,
        name: str,
        files: dict[str, bytes],
        issues: FakePaginatedItems | None = None,
        pull_requests: FakePaginatedItems | None = None,
    ) -> None:
        self.id = repository_id
        self.html_url = f"https://github.example/{name}"
        self.files = files
        self.issues = issues or FakePaginatedItems([])
        self.pull_requests = pull_requests or FakePaginatedItems([])
        self.tree_calls: list[tuple[str, bool]] = []
        self.content_calls: list[tuple[str, str]] = []
        self.issue_calls: list[dict[str, str]] = []
        self.pull_request_calls: list[dict[str, str]] = []

    def get_git_tree(self, tree_id: str, *, recursive: bool) -> Any:
        self.tree_calls.append((tree_id, recursive))
        prefix = "" if tree_id in {"main", "release"} else tree_id.removeprefix("tree:")
        directories: set[str] = set()
        files: list[str] = []
        for full_path in self.files:
            if prefix and not full_path.startswith(f"{prefix}/"):
                continue
            relative_path = full_path[len(prefix) + 1 :] if prefix else full_path
            child_name, separator, _ = relative_path.partition("/")
            if separator:
                directories.add(child_name)
            else:
                files.append(child_name)
        tree = [
            SimpleNamespace(
                type="tree",
                path=name,
                sha=f"tree:{prefix}/{name}" if prefix else f"tree:{name}",
            )
            for name in sorted(directories)
        ]
        tree.extend(SimpleNamespace(type="blob", path=name, sha=f"blob:{name}") for name in sorted(set(files)))
        return SimpleNamespace(truncated=False, tree=tree)

    def get_contents(self, path: str, *, ref: str) -> Any:
        self.content_calls.append((path, ref))
        return SimpleNamespace(decoded_content=self.files[path])

    def get_issues(self, *, state: str) -> FakePaginatedItems:
        self.issue_calls.append({"state": state})
        return self.issues

    def get_pulls(self, *, state: str) -> FakePaginatedItems:
        self.pull_request_calls.append({"state": state})
        return self.pull_requests


class FakeGithub:
    """GitHub client that resolves configured names to returned repositories."""

    def __init__(self, repositories: dict[str, FakeRepository]) -> None:
        self.repositories = repositories
        self.requested: list[str] = []

    def get_repo(self, full_name: str) -> FakeRepository:
        self.requested.append(full_name)
        return self.repositories[full_name]


def github_context(config: GitHubConnectorConfiguration, session: FakeSourceSession) -> Any:
    return ConnectorContext(
        connector_configuration=config,
        job_configuration=EmptyJobConfiguration(),
        source_session=session,
    )


class FakeGithubEntry(SimpleNamespace):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pull_request_access_count = 0

    @property
    def pull_request(self) -> Any:
        self.pull_request_access_count += 1
        return self.raw_data.get("pull_request")


def github_entry(
    number: int,
    title: str,
    state: str = "open",
    body: str | None = "Body",
    author: str | None = "octocat",
    pull_request: bool = False,
) -> Any:
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    updated_at = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    return FakeGithubEntry(
        number=number,
        title=title,
        state=state,
        body=body,
        user=SimpleNamespace(login=author) if author is not None else None,
        created_at=created_at,
        updated_at=updated_at,
        html_url=f"https://github.example/items/{number}",
        raw_data={"pull_request": {"url": "duplicate"}} if pull_request else {},
    )


def test_configuration_is_strict_frozen_and_uses_shared_empty_job_model() -> None:
    config = GitHubConnectorConfiguration(
        token_secret_arn="arn:secret",
        repositories=(GitHubRepositoryConfiguration(full_name="group/one"),),
        api_url="https://github.enterprise.example:8443/api/v3",
    )

    assert connector.definition.job_configuration_model is EmptyJobConfiguration
    with pytest.raises(ValidationError):
        GitHubConnectorConfiguration.model_validate(
            {"token_secret_arn": "arn:secret", "repositories": [], "unexpected": True}
        )
    with pytest.raises(ValidationError):
        config.api_url = "https://changed.example"  # type: ignore[misc]


@pytest.mark.parametrize(
    "api_url",
    (
        "http://github.example/api/v3",
        "https://token@github.example/api/v3",
        "https://github.example/api/v3?token=unsafe",
        "https://github.example/api/v3#fragment",
        "https:///api/v3",
        " https://github.example/api/v3",
        "https://github.example/api/v3 ",
        "\nhttps://github.example/api/v3",
        "https://github.example/api/v3\r",
        "\thttps://github.example/api/v3",
        "https://github.example\n.evil/api/v3",
        "https://github.example/api\t/v3",
    ),
)
def test_configuration_rejects_unsafe_api_url_before_secret_retrieval(
    api_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_session = FakeSourceSession("local-token")
    harness = RuntimeHarness(monkeypatch, DestinationMode.S3, source_session)
    configuration_json = json.dumps(
        {
            "token_secret_arn": "arn:secret",
            "repositories": [{"full_name": "group/one"}],
            "api_url": api_url,
        }
    )

    with pytest.raises(SystemExit) as caught:
        harness.run_configuration_json("github_connector.connector:definition", configuration_json)

    assert caught.value.code == 1
    assert harness.control_session.sts.requests == []
    assert source_session.client_names == []
    assert source_session.secret_ids == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ccf-connector: connector run failed\n"


def test_documents_are_lazy_and_cover_multiple_repositories_and_nested_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = {
        "group/one": FakeRepository(101, "group/one", {"README.md": b"one", "src/app.py": b"app"}),
        "group/two": FakeRepository(202, "group/two", {"src/app.py": b"two"}),
    }
    github = FakeGithub(repositories)
    monkeypatch.setattr(connector, "Github", lambda **_: github)
    session = FakeSourceSession("local-token")
    config = GitHubConnectorConfiguration(
        token_secret_arn="arn:secret",
        repositories=tuple(GitHubRepositoryConfiguration(full_name=name) for name in repositories),
    )

    generated = connector.documents(github_context(config, session))
    assert github.requested == []
    snapshot = list(generated)

    assert session.client_names == ["secretsmanager"]
    assert session.secret_ids == ["arn:secret"]
    assert github.requested == ["group/one", "group/two"]
    assert {document.metadata.title for document in snapshot} == {"README.md", "src/app.py"}
    assert len(snapshot) == 3
    assert all(call[1] is False for repository in repositories.values() for call in repository.tree_calls)
    assert all(repository.issue_calls == [{"state": "all"}] for repository in repositories.values())
    assert all(repository.pull_request_calls == [{"state": "all"}] for repository in repositories.values())


def test_document_identity_uses_returned_repository_id_not_configured_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renamed = FakeRepository(101, "new/name", {"src/app.py": b"same"})
    other = FakeRepository(202, "other/name", {"src/app.py": b"same"})
    github = FakeGithub({"old/name": renamed, "new/name": renamed, "other/name": other})
    monkeypatch.setattr(connector, "Github", lambda **_: github)

    def document_id(name: str) -> str:
        config = GitHubConnectorConfiguration(
            token_secret_arn="arn:secret",
            repositories=(GitHubRepositoryConfiguration(full_name=name, file_extensions=(".py",)),),
        )
        return next(connector.documents(github_context(config, FakeSourceSession("local-token")))).id

    assert document_id("old/name") == document_id("new/name")
    assert document_id("new/name") != document_id("other/name")
    assert document_id("new/name") == "github_64a78b70464a5025f6c133e8f50dffa970bd13995e017bec92442e4d61e5e6e2"


def test_issue_and_pull_request_documents_have_exact_content_metadata_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = github_entry(7, "Issue café", state="closed", body="Issue body résumé", author="issue-author")
    duplicate_pull_request = github_entry(8, "Duplicate pull", pull_request=True)
    pull_request = github_entry(8, "Pull title", body="Pull body", author="pull-author")
    repository = FakeRepository(
        101,
        "group/one",
        {},
        issues=FakePaginatedItems([issue, duplicate_pull_request]),
        pull_requests=FakePaginatedItems([pull_request]),
    )
    github = FakeGithub({"group/one": repository})
    monkeypatch.setattr(connector, "Github", lambda **_: github)
    config = GitHubConnectorConfiguration(
        token_secret_arn="arn:secret",
        repositories=(GitHubRepositoryConfiguration(full_name="group/one"),),
    )

    snapshot = list(connector.documents(github_context(config, FakeSourceSession("local-token"))))

    assert issue.pull_request_access_count == 0
    assert repository.issue_calls == [{"state": "all"}]
    assert repository.pull_request_calls == [{"state": "all"}]
    assert [document.filename for document in snapshot] == ["issue-7.md", "pull-request-8.md"]
    assert snapshot[0].id == "github_763973e1abf040711a4c53ec33b363b6cca76c5e0a6209f14ae9a76a012fd39d"
    assert snapshot[0].content == (
        b"# Issue caf\xc3\xa9\n\n"
        b"- Number: 7\n"
        b"- State: closed\n"
        b"- Author: issue-author\n"
        b"- Created: 2026-01-02T03:04:05+00:00\n"
        b"- Updated: 2026-02-03T04:05:06+00:00\n\n"
        b"Issue body r\xc3\xa9sum\xc3\xa9\n"
    )
    assert snapshot[0].metadata.model_dump(exclude_none=True, exclude={"access_control"}) == {
        "title": "Issue café",
        "source_uri": "https://github.example/items/7",
        "last_updated_at": "2026-02-03T04:05:06+00:00",
        "created_at": "2026-01-02T03:04:05+00:00",
        "attributes": {"provider": "github", "category": "issue", "state": "closed"},
    }
    assert snapshot[1].id == "github_28d60639f98600e4f64dd449fa0135b96419257ab7e2e54f8fcd7f631b7d7fe2"
    assert snapshot[1].content == (
        b"# Pull title\n\n"
        b"- Number: 8\n"
        b"- State: open\n"
        b"- Author: pull-author\n"
        b"- Created: 2026-01-02T03:04:05+00:00\n"
        b"- Updated: 2026-02-03T04:05:06+00:00\n\n"
        b"Pull body\n"
    )
    assert snapshot[1].metadata.model_dump(exclude_none=True, exclude={"access_control"}) == {
        "title": "Pull title",
        "source_uri": "https://github.example/items/8",
        "last_updated_at": "2026-02-03T04:05:06+00:00",
        "created_at": "2026-01-02T03:04:05+00:00",
        "attributes": {"provider": "github", "category": "pull_request", "state": "open"},
    }


def test_category_documents_map_missing_body_and_author_to_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository(
        101,
        "group/one",
        {},
        issues=FakePaginatedItems([github_entry(7, "Issue title", body=None, author=None)]),
        pull_requests=FakePaginatedItems([github_entry(8, "Pull title", body=None, author=None)]),
    )
    monkeypatch.setattr(connector, "Github", lambda **_: FakeGithub({"group/one": repository}))
    config = GitHubConnectorConfiguration(
        token_secret_arn="arn:secret",
        repositories=(GitHubRepositoryConfiguration(full_name="group/one"),),
    )

    snapshot = list(connector.documents(github_context(config, FakeSourceSession("local-token"))))

    assert len(snapshot) == 2
    assert all(b"- Author: \n" in document.content for document in snapshot)
    assert all(document.content.endswith(b"\n\n\n") for document in snapshot)


def test_category_identity_uses_returned_repository_id_category_and_number() -> None:
    assert connector._category_document_id("101", "issue", 7) == (
        "github_763973e1abf040711a4c53ec33b363b6cca76c5e0a6209f14ae9a76a012fd39d"
    )
    assert connector._category_document_id("101", "issue", 7) != connector._category_document_id("202", "issue", 7)
    assert connector._category_document_id("101", "issue", 7) != connector._category_document_id(
        "101", "pull_request", 7
    )
    assert connector._category_document_id("101", "issue", 7) != connector._category_document_id("101", "issue", 8)
