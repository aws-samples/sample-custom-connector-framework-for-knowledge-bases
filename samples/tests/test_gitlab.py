"""Focused mapping tests for the GitLab connector definition."""

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from ccf_api_models.models import DestinationMode
from custom_connector_framework import ConnectorContext, EmptyJobConfiguration
from pydantic import ValidationError

from gitlab_connector import connector
from gitlab_connector.connector import GitLabConnectorConfiguration, GitLabProjectConfiguration
from tests.runtime_harness import RuntimeHarness


class FakeSourceSession:
    """Source session that records credential access."""

    def __init__(self) -> None:
        self.client_names: list[str] = []
        self.secret_ids: list[str] = []

    def client(self, service_name: str) -> Any:
        self.client_names.append(service_name)
        return self

    def get_secret_value(self, **kwargs: str) -> dict[str, str]:
        self.secret_ids.append(kwargs["SecretId"])
        return {"SecretString": "token"}


class FakeFiles:
    """GitLab files endpoint backed by a byte map."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.calls: list[tuple[str, str]] = []

    def get(self, *, file_path: str, ref: str) -> Any:
        self.calls.append((file_path, ref))
        return SimpleNamespace(decode=lambda: self.files[file_path])


class FakeListEndpoint:
    """Lazy python-gitlab list endpoint with optional partial failure."""

    def __init__(self, entries: list[Any], fail_after: int | None = None) -> None:
        self.entries = entries
        self.fail_after = fail_after
        self.calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> Iterator[Any]:
        self.calls.append(kwargs)
        for index, entry in enumerate(self.entries):
            if self.fail_after == index:
                raise RuntimeError("private GitLab enumeration failure")
            yield entry
        if self.fail_after == len(self.entries):
            raise RuntimeError("private GitLab enumeration failure")


class FakeProject:
    """GitLab project with an immutable API identity and paginated tree."""

    def __init__(
        self,
        project_id: int,
        name: str,
        files: dict[str, bytes],
        issues: FakeListEndpoint | None = None,
        merge_requests: FakeListEndpoint | None = None,
    ) -> None:
        self.id = project_id
        self.web_url = f"https://gitlab.example/{name}"
        self.files = FakeFiles(files)
        self.issues = issues or FakeListEndpoint([])
        self.mergerequests = merge_requests or FakeListEndpoint([])
        self.tree_calls: list[dict[str, object]] = []

    def repository_tree(self, **kwargs: object) -> list[dict[str, str]]:
        self.tree_calls.append(kwargs)
        return [{"type": "blob", "path": path} for path in sorted(self.files.files)]


class FakeGitlab:
    """GitLab client that resolves configured project names."""

    def __init__(self, projects: dict[str, FakeProject]) -> None:
        self._projects = projects
        self.requested: list[str] = []
        self.projects = self

    def get(self, project_id: str) -> FakeProject:
        self.requested.append(project_id)
        return self._projects[project_id]


def gitlab_context(config: GitLabConnectorConfiguration, session: FakeSourceSession) -> Any:
    return ConnectorContext(
        connector_configuration=config,
        job_configuration=EmptyJobConfiguration(),
        source_session=session,
    )


def gitlab_entry(
    iid: int,
    title: str,
    state: str = "opened",
    description: str | None = "Description",
    author: str | None = "gitlab-user",
) -> Any:
    return SimpleNamespace(
        iid=iid,
        title=title,
        state=state,
        description=description,
        author={"username": author} if author is not None else None,
        created_at="2026-01-02T03:04:05.000+00:00",
        updated_at="2026-02-03T04:05:06.000+00:00",
        web_url=f"https://gitlab.example/items/{iid}",
    )


def test_configuration_is_strict_frozen_and_uses_shared_empty_job_model() -> None:
    config = GitLabConnectorConfiguration(
        token_secret_arn="arn:secret",
        projects=(GitLabProjectConfiguration(project_id="group/one"),),
        server_url="https://gitlab.enterprise.example:8443/base/path",
    )

    assert connector.definition.job_configuration_model is EmptyJobConfiguration
    with pytest.raises(ValidationError):
        GitLabConnectorConfiguration.model_validate(
            {"token_secret_arn": "arn:secret", "projects": [], "unexpected": True}
        )
    with pytest.raises(ValidationError):
        config.server_url = "https://changed.example"  # type: ignore[misc]


@pytest.mark.parametrize(
    "server_url",
    (
        "http://gitlab.example",
        "https://token@gitlab.example",
        "https://gitlab.example/base?token=unsafe",
        "https://gitlab.example/base#fragment",
        "https:///base",
        " https://gitlab.example/base",
        "https://gitlab.example/base ",
        "\nhttps://gitlab.example/base",
        "https://gitlab.example/base\r",
        "\thttps://gitlab.example/base",
        "https://gitlab.example\n.evil/base",
        "https://gitlab.example/base\t/path",
    ),
)
def test_configuration_rejects_unsafe_server_url_before_secret_retrieval(
    server_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_session = FakeSourceSession()
    harness = RuntimeHarness(monkeypatch, DestinationMode.S3, source_session)
    configuration_json = json.dumps(
        {
            "token_secret_arn": "arn:secret",
            "projects": [{"project_id": "group/one"}],
            "server_url": server_url,
        }
    )

    with pytest.raises(SystemExit) as caught:
        harness.run_configuration_json("gitlab_connector.connector:definition", configuration_json)

    assert caught.value.code == 1
    assert harness.control_session.sts.requests == []
    assert source_session.client_names == []
    assert source_session.secret_ids == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ccf-connector: connector run failed\n"


def test_documents_are_lazy_and_auto_paginate_multiple_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    projects = {
        "group/one": FakeProject(1, "group/one", {"README.md": b"one", "src/app.py": b"app"}),
        "group/two": FakeProject(2, "group/two", {"src/app.py": b"two"}),
    }
    client = FakeGitlab(projects)
    monkeypatch.setattr(connector.gitlab, "Gitlab", lambda **_: client)
    session = FakeSourceSession()
    config = GitLabConnectorConfiguration(
        token_secret_arn="arn:secret",
        projects=tuple(GitLabProjectConfiguration(project_id=name) for name in projects),
    )

    generated = connector.documents(gitlab_context(config, session))
    assert client.requested == []
    snapshot = list(generated)

    assert session.client_names == ["secretsmanager"]
    assert session.secret_ids == ["arn:secret"]
    assert client.requested == ["group/one", "group/two"]
    assert len(snapshot) == 3
    assert all(
        project.tree_calls == [{"ref": "main", "recursive": True, "iterator": True, "per_page": 100}]
        for project in projects.values()
    )
    expected_list_call = [{"state": "all", "iterator": True, "per_page": 100}]
    assert all(project.issues.calls == expected_list_call for project in projects.values())
    assert all(project.mergerequests.calls == expected_list_call for project in projects.values())


def test_document_identity_uses_returned_project_id_not_configured_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renamed = FakeProject(1, "new/name", {"src/app.py": b"same"})
    other = FakeProject(2, "other/name", {"src/app.py": b"same"})
    client = FakeGitlab({"old/name": renamed, "new/name": renamed, "other/name": other})
    monkeypatch.setattr(connector.gitlab, "Gitlab", lambda **_: client)

    def document_id(name: str) -> str:
        config = GitLabConnectorConfiguration(
            token_secret_arn="arn:secret",
            projects=(GitLabProjectConfiguration(project_id=name, file_extensions=(".py",)),),
        )
        return next(connector.documents(gitlab_context(config, FakeSourceSession()))).id

    assert document_id("old/name") == document_id("new/name")
    assert document_id("new/name") != document_id("other/name")
    assert document_id("new/name") == "gitlab_33a43bb55a0d559a252cbb717b4bcad25435edfce7c3480dbe3596d367ba3b27"


def test_issue_and_merge_request_documents_have_exact_content_metadata_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = gitlab_entry(7, "Issue café", state="closed", description="Issue résumé", author="issue-author")
    merge_request = gitlab_entry(8, "Merge title", description="Merge description", author="merge-author")
    project = FakeProject(
        1,
        "group/one",
        {},
        issues=FakeListEndpoint([issue]),
        merge_requests=FakeListEndpoint([merge_request]),
    )
    client = FakeGitlab({"group/one": project})
    monkeypatch.setattr(connector.gitlab, "Gitlab", lambda **_: client)
    config = GitLabConnectorConfiguration(
        token_secret_arn="arn:secret",
        projects=(GitLabProjectConfiguration(project_id="group/one"),),
    )

    snapshot = list(connector.documents(gitlab_context(config, FakeSourceSession())))

    expected_list_call = [{"state": "all", "iterator": True, "per_page": 100}]
    assert project.issues.calls == expected_list_call
    assert project.mergerequests.calls == expected_list_call
    assert [document.filename for document in snapshot] == ["issue-7.md", "merge-request-8.md"]
    assert snapshot[0].id == "gitlab_56b6f68f6c84947db1a223176aa83154f8eb13f5f0ba5acef94cf189a816eb4d"
    assert snapshot[0].content == (
        b"# Issue caf\xc3\xa9\n\n"
        b"- IID: 7\n"
        b"- State: closed\n"
        b"- Author: issue-author\n"
        b"- Created: 2026-01-02T03:04:05.000+00:00\n"
        b"- Updated: 2026-02-03T04:05:06.000+00:00\n\n"
        b"Issue r\xc3\xa9sum\xc3\xa9\n"
    )
    assert snapshot[0].metadata.model_dump(exclude_none=True, exclude={"access_control"}) == {
        "title": "Issue café",
        "source_uri": "https://gitlab.example/items/7",
        "last_updated_at": "2026-02-03T04:05:06.000+00:00",
        "created_at": "2026-01-02T03:04:05.000+00:00",
        "attributes": {"provider": "gitlab", "category": "issue", "state": "closed"},
    }
    assert snapshot[1].id == "gitlab_32e0746c5fb8e368bce839a3217c00b5e8304d1996977bb44233e4f36f1f7459"
    assert snapshot[1].content == (
        b"# Merge title\n\n"
        b"- IID: 8\n"
        b"- State: opened\n"
        b"- Author: merge-author\n"
        b"- Created: 2026-01-02T03:04:05.000+00:00\n"
        b"- Updated: 2026-02-03T04:05:06.000+00:00\n\n"
        b"Merge description\n"
    )
    assert snapshot[1].metadata.model_dump(exclude_none=True, exclude={"access_control"}) == {
        "title": "Merge title",
        "source_uri": "https://gitlab.example/items/8",
        "last_updated_at": "2026-02-03T04:05:06.000+00:00",
        "created_at": "2026-01-02T03:04:05.000+00:00",
        "attributes": {"provider": "gitlab", "category": "merge_request", "state": "opened"},
    }


def test_category_documents_map_missing_description_and_author_to_empty_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = FakeProject(
        1,
        "group/one",
        {},
        issues=FakeListEndpoint([gitlab_entry(7, "Issue title", description=None, author=None)]),
        merge_requests=FakeListEndpoint([gitlab_entry(8, "Merge title", description=None, author=None)]),
    )
    monkeypatch.setattr(connector.gitlab, "Gitlab", lambda **_: FakeGitlab({"group/one": project}))
    config = GitLabConnectorConfiguration(
        token_secret_arn="arn:secret",
        projects=(GitLabProjectConfiguration(project_id="group/one"),),
    )

    snapshot = list(connector.documents(gitlab_context(config, FakeSourceSession())))

    assert len(snapshot) == 2
    assert all(b"- Author: \n" in document.content for document in snapshot)
    assert all(document.content.endswith(b"\n\n\n") for document in snapshot)


def test_category_identity_uses_returned_project_id_category_and_iid() -> None:
    assert connector._category_document_id("1", "issue", 7) == (
        "gitlab_56b6f68f6c84947db1a223176aa83154f8eb13f5f0ba5acef94cf189a816eb4d"
    )
    assert connector._category_document_id("1", "issue", 7) != connector._category_document_id("2", "issue", 7)
    assert connector._category_document_id("1", "issue", 7) != connector._category_document_id("1", "merge_request", 7)
    assert connector._category_document_id("1", "issue", 7) != connector._category_document_id("1", "issue", 8)
