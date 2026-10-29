"""Local end-to-end tests through the generic connector launcher."""

import json
from typing import Any

import pytest
from ccf_api_models.models import DestinationMode, DocumentSummary
from custom_connector_framework._safe_http import PublicHttpResponse
from custom_connector_framework.providers.quick import QuickKnowledgeBaseAdapter

from github_connector import connector as github_connector
from github_connector.connector import GitHubConnectorConfiguration, GitHubRepositoryConfiguration
from gitlab_connector import connector as gitlab_connector
from gitlab_connector.connector import GitLabConnectorConfiguration, GitLabProjectConfiguration
from tests.runtime_harness import RuntimeHarness
from tests.test_github import FakeGithub, FakePaginatedItems, FakeRepository, github_entry
from tests.test_github import FakeSourceSession as GitHubSourceSession
from tests.test_gitlab import FakeGitlab, FakeListEndpoint, FakeProject, gitlab_entry
from tests.test_gitlab import FakeSourceSession as GitLabSourceSession
from web_crawler import connector as crawler_connector
from web_crawler.connector import CrawlerConnectorConfiguration, CrawlUrl

_MANAGED_MODES = (DestinationMode.BEDROCK, DestinationMode.QUICK)


def _mutations(harness: RuntimeHarness) -> tuple[int, int, int, int]:
    s3 = harness.control_session.s3
    return s3.put_count, s3.delete_count, harness.client.put_batches, harness.client.delete_batches


def _assert_one_sidecar_per_document(harness: RuntimeHarness) -> None:
    documents = harness.client.state.values()
    object_keys = {document.object_key for document in documents}
    sidecar_keys = {document.sidecar_key for document in documents}
    assert len(object_keys) == len(documents)
    assert len(sidecar_keys) == len(documents)
    assert None not in sidecar_keys
    assert {key for key in harness.control_session.s3.objects if key.endswith(".metadata.json")} == sidecar_keys
    assert set(harness.control_session.s3.objects) == object_keys | sidecar_keys


def _single_github_runtime(
    monkeypatch: pytest.MonkeyPatch,
    mode: DestinationMode,
) -> tuple[RuntimeHarness, GitHubConnectorConfiguration, str]:
    repositories = {"group/one": FakeRepository(101, "group/one", {"src/app.py": b"one"})}
    monkeypatch.setattr(github_connector, "Github", lambda **_: FakeGithub(repositories))
    configuration = GitHubConnectorConfiguration(
        token_secret_arn="arn:secret",
        repositories=(GitHubRepositoryConfiguration(full_name="group/one", file_extensions=(".py",)),),
    )
    return (
        RuntimeHarness(monkeypatch, mode, GitHubSourceSession("local-token")),
        configuration,
        "github_connector.connector:definition",
    )


def _assert_removed(harness: RuntimeHarness, document: DocumentSummary) -> None:
    assert document.document_id not in harness.client.state
    assert document.object_key not in harness.control_session.s3.objects
    assert document.sidecar_key not in harness.control_session.s3.objects


@pytest.mark.parametrize("mode", _MANAGED_MODES)
def test_github_launcher_reconciles_multiple_repositories_and_all_categories(
    monkeypatch: pytest.MonkeyPatch,
    mode: DestinationMode,
) -> None:
    repositories = {
        "group/one": FakeRepository(
            101,
            "group/one",
            {"src/app.py": b"one", "src/deleted.py": b"delete"},
            issues=FakePaginatedItems([github_entry(11, "Issue one"), github_entry(12, "Deleted issue")]),
            pull_requests=FakePaginatedItems([github_entry(21, "Pull one"), github_entry(22, "Deleted pull")]),
        ),
        "group/two": FakeRepository(
            202,
            "group/two",
            {"src/app.py": b"two"},
            issues=FakePaginatedItems([github_entry(31, "Issue two")]),
            pull_requests=FakePaginatedItems([github_entry(41, "Pull two")]),
        ),
    }
    monkeypatch.setattr(github_connector, "Github", lambda **_: FakeGithub(repositories))
    harness = RuntimeHarness(monkeypatch, mode, GitHubSourceSession("local-token"))
    both_repositories = GitHubConnectorConfiguration(
        token_secret_arn="arn:secret",
        repositories=tuple(
            GitHubRepositoryConfiguration(full_name=name, file_extensions=(".py",)) for name in repositories
        ),
    )
    first_repository = GitHubConnectorConfiguration(
        token_secret_arn="arn:secret",
        repositories=(GitHubRepositoryConfiguration(full_name="group/one", file_extensions=(".py",)),),
    )
    target = "github_connector.connector:definition"
    first_ids = {
        github_connector._document_id("101", "src/app.py"),
        github_connector._document_id("101", "src/deleted.py"),
        github_connector._category_document_id("101", "issue", 11),
        github_connector._category_document_id("101", "issue", 12),
        github_connector._category_document_id("101", "pull_request", 21),
        github_connector._category_document_id("101", "pull_request", 22),
    }
    second_ids = {
        github_connector._document_id("202", "src/app.py"),
        github_connector._category_document_id("202", "issue", 31),
        github_connector._category_document_id("202", "pull_request", 41),
    }

    assert harness.run(target, both_repositories) == 0
    assert set(harness.client.state) == first_ids | second_ids
    assert _mutations(harness) == (18, 0, 1, 0)
    _assert_one_sidecar_per_document(harness)

    repositories["group/one"].files["src/added.py"] = b"added"
    repositories["group/one"].issues.entries.append(github_entry(13, "Added issue"))
    repositories["group/one"].pull_requests.entries.append(github_entry(23, "Added pull"))
    assert harness.run(target, both_repositories) == 0
    added_ids = {
        github_connector._document_id("101", "src/added.py"),
        github_connector._category_document_id("101", "issue", 13),
        github_connector._category_document_id("101", "pull_request", 23),
    }
    assert set(harness.client.state) == first_ids | second_ids | added_ids
    assert _mutations(harness) == (24, 0, 2, 0)
    _assert_one_sidecar_per_document(harness)
    state_before_updates = dict(harness.client.state)

    repositories["group/one"].files["src/app.py"] = b"changed"
    del repositories["group/one"].files["src/deleted.py"]
    repositories["group/one"].issues.entries[0] = github_entry(
        11, "Updated issue", state="closed", body="Updated issue body"
    )
    del repositories["group/one"].issues.entries[1]
    repositories["group/one"].pull_requests.entries[0] = github_entry(
        21, "Updated pull", state="closed", body="Updated pull body"
    )
    del repositories["group/one"].pull_requests.entries[1]
    updated_ids = {
        github_connector._document_id("101", "src/app.py"),
        github_connector._category_document_id("101", "issue", 11),
        github_connector._category_document_id("101", "pull_request", 21),
    }
    deleted_ids = {
        github_connector._document_id("101", "src/deleted.py"),
        github_connector._category_document_id("101", "issue", 12),
        github_connector._category_document_id("101", "pull_request", 22),
    }

    assert harness.run(target, both_repositories) == 0
    assert set(harness.client.state) == (first_ids | second_ids | added_ids) - deleted_ids
    assert _mutations(harness) == (30, 12, 3, 1)
    assert all(
        harness.client.state[document_id].checksum != state_before_updates[document_id].checksum
        for document_id in updated_ids
    )
    for document_id in deleted_ids:
        _assert_removed(harness, state_before_updates[document_id])
    _assert_one_sidecar_per_document(harness)
    second_documents = {document_id: harness.client.state[document_id] for document_id in second_ids}

    assert harness.run(target, first_repository) == 0
    assert set(harness.client.state).isdisjoint(second_ids)
    assert _mutations(harness) == (30, 18, 3, 2)
    for document in second_documents.values():
        _assert_removed(harness, document)
    _assert_one_sidecar_per_document(harness)
    state_before_rerun = dict(harness.client.state)
    mutations_before_rerun = _mutations(harness)

    assert harness.run(target, first_repository) == 0
    assert harness.client.state == state_before_rerun
    assert _mutations(harness) == mutations_before_rerun
    _assert_one_sidecar_per_document(harness)
    assert len(harness.control_session.sts.requests) == 5


@pytest.mark.parametrize("mode", _MANAGED_MODES)
def test_gitlab_launcher_reconciles_multiple_projects_and_all_categories(
    monkeypatch: pytest.MonkeyPatch,
    mode: DestinationMode,
) -> None:
    projects = {
        "group/one": FakeProject(
            1,
            "group/one",
            {"src/app.py": b"one", "src/deleted.py": b"delete"},
            issues=FakeListEndpoint([gitlab_entry(11, "Issue one"), gitlab_entry(12, "Deleted issue")]),
            merge_requests=FakeListEndpoint([gitlab_entry(21, "Merge one"), gitlab_entry(22, "Deleted merge")]),
        ),
        "group/two": FakeProject(
            2,
            "group/two",
            {"src/app.py": b"two"},
            issues=FakeListEndpoint([gitlab_entry(31, "Issue two")]),
            merge_requests=FakeListEndpoint([gitlab_entry(41, "Merge two")]),
        ),
    }
    monkeypatch.setattr(gitlab_connector.gitlab, "Gitlab", lambda **_: FakeGitlab(projects))
    harness = RuntimeHarness(monkeypatch, mode, GitLabSourceSession())
    both_projects = GitLabConnectorConfiguration(
        token_secret_arn="arn:secret",
        projects=tuple(GitLabProjectConfiguration(project_id=name, file_extensions=(".py",)) for name in projects),
    )
    first_project = GitLabConnectorConfiguration(
        token_secret_arn="arn:secret",
        projects=(GitLabProjectConfiguration(project_id="group/one", file_extensions=(".py",)),),
    )
    target = "gitlab_connector.connector:definition"
    first_ids = {
        gitlab_connector._document_id("1", "src/app.py"),
        gitlab_connector._document_id("1", "src/deleted.py"),
        gitlab_connector._category_document_id("1", "issue", 11),
        gitlab_connector._category_document_id("1", "issue", 12),
        gitlab_connector._category_document_id("1", "merge_request", 21),
        gitlab_connector._category_document_id("1", "merge_request", 22),
    }
    second_ids = {
        gitlab_connector._document_id("2", "src/app.py"),
        gitlab_connector._category_document_id("2", "issue", 31),
        gitlab_connector._category_document_id("2", "merge_request", 41),
    }

    assert harness.run(target, both_projects) == 0
    assert set(harness.client.state) == first_ids | second_ids
    assert _mutations(harness) == (18, 0, 1, 0)
    _assert_one_sidecar_per_document(harness)

    projects["group/one"].files.files["src/added.py"] = b"added"
    projects["group/one"].issues.entries.append(gitlab_entry(13, "Added issue"))
    projects["group/one"].mergerequests.entries.append(gitlab_entry(23, "Added merge"))
    assert harness.run(target, both_projects) == 0
    added_ids = {
        gitlab_connector._document_id("1", "src/added.py"),
        gitlab_connector._category_document_id("1", "issue", 13),
        gitlab_connector._category_document_id("1", "merge_request", 23),
    }
    assert set(harness.client.state) == first_ids | second_ids | added_ids
    assert _mutations(harness) == (24, 0, 2, 0)
    _assert_one_sidecar_per_document(harness)
    state_before_updates = dict(harness.client.state)

    projects["group/one"].files.files["src/app.py"] = b"changed"
    del projects["group/one"].files.files["src/deleted.py"]
    projects["group/one"].issues.entries[0] = gitlab_entry(
        11, "Updated issue", state="closed", description="Updated issue description"
    )
    del projects["group/one"].issues.entries[1]
    projects["group/one"].mergerequests.entries[0] = gitlab_entry(
        21, "Updated merge", state="merged", description="Updated merge description"
    )
    del projects["group/one"].mergerequests.entries[1]
    updated_ids = {
        gitlab_connector._document_id("1", "src/app.py"),
        gitlab_connector._category_document_id("1", "issue", 11),
        gitlab_connector._category_document_id("1", "merge_request", 21),
    }
    deleted_ids = {
        gitlab_connector._document_id("1", "src/deleted.py"),
        gitlab_connector._category_document_id("1", "issue", 12),
        gitlab_connector._category_document_id("1", "merge_request", 22),
    }

    assert harness.run(target, both_projects) == 0
    assert set(harness.client.state) == (first_ids | second_ids | added_ids) - deleted_ids
    assert _mutations(harness) == (30, 12, 3, 1)
    assert all(
        harness.client.state[document_id].checksum != state_before_updates[document_id].checksum
        for document_id in updated_ids
    )
    for document_id in deleted_ids:
        _assert_removed(harness, state_before_updates[document_id])
    _assert_one_sidecar_per_document(harness)
    second_documents = {document_id: harness.client.state[document_id] for document_id in second_ids}

    assert harness.run(target, first_project) == 0
    assert set(harness.client.state).isdisjoint(second_ids)
    assert _mutations(harness) == (30, 18, 3, 2)
    for document in second_documents.values():
        _assert_removed(harness, document)
    _assert_one_sidecar_per_document(harness)
    state_before_rerun = dict(harness.client.state)
    mutations_before_rerun = _mutations(harness)

    assert harness.run(target, first_project) == 0
    assert harness.client.state == state_before_rerun
    assert _mutations(harness) == mutations_before_rerun
    _assert_one_sidecar_per_document(harness)
    assert len(harness.control_session.sts.requests) == 5


def test_crawler_launcher_reconciles_page_updates_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    first_url = "https://example.com/first"
    removed_url = "https://example.com/removed"
    pages = {first_url: b"<title>First</title>", removed_url: b"<title>Removed</title>"}

    def fetch(url: str) -> PublicHttpResponse:
        return PublicHttpResponse(content=pages[url], content_type="text/html", final_url=url)

    monkeypatch.setattr(crawler_connector, "fetch_public_https", fetch)
    harness = RuntimeHarness(monkeypatch, DestinationMode.BEDROCK, object())
    target = "web_crawler.connector:definition"
    current = CrawlerConnectorConfiguration(urls=(CrawlUrl(url=first_url),))

    assert (
        harness.run(target, CrawlerConnectorConfiguration(urls=(CrawlUrl(url=first_url), CrawlUrl(url=removed_url))))
        == 0
    )
    assert len(harness.client.state) == 2
    assert _mutations(harness) == (4, 0, 1, 0)
    initial_state = dict(harness.client.state)

    pages[first_url] = b"<title>Updated</title>"
    assert harness.run(target, current) == 0
    assert len(harness.client.state) == 1
    assert _mutations(harness) == (6, 4, 2, 1)
    assert {document.checksum for document in harness.client.state.values()}.isdisjoint(
        document.checksum for document in initial_state.values()
    )
    removed_id = next(iter(set(initial_state) - set(harness.client.state)))
    _assert_removed(harness, initial_state[removed_id])
    state_before_rerun = dict(harness.client.state)
    mutations_before_rerun = _mutations(harness)
    assert harness.run(target, current) == 0
    assert harness.client.state == state_before_rerun
    assert _mutations(harness) == mutations_before_rerun
    _assert_one_sidecar_per_document(harness)


@pytest.mark.parametrize("managed_mode", _MANAGED_MODES)
def test_reconciliation_s3_to_managed_creates_one_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    managed_mode: DestinationMode,
) -> None:
    harness, configuration, target = _single_github_runtime(monkeypatch, DestinationMode.S3)
    assert harness.run(target, configuration) == 0
    tracked_before = next(iter(harness.client.state.values()))
    assert tracked_before.sidecar_key is None

    harness.client.mode = managed_mode
    assert harness.run(target, configuration) == 0

    tracked_after = next(iter(harness.client.state.values()))
    assert tracked_after.object_key != tracked_before.object_key
    assert tracked_before.object_key not in harness.control_session.s3.objects
    assert tracked_after.checksum != tracked_before.checksum
    _assert_one_sidecar_per_document(harness)


def test_reconciliation_managed_to_s3_removes_old_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    harness, configuration, target = _single_github_runtime(monkeypatch, DestinationMode.BEDROCK)
    assert harness.run(target, configuration) == 0
    tracked_before = next(iter(harness.client.state.values()))
    assert tracked_before.sidecar_key is not None

    harness.client.mode = DestinationMode.S3
    assert harness.run(target, configuration) == 0

    tracked_after = next(iter(harness.client.state.values()))
    assert tracked_after.sidecar_key is None
    assert tracked_before.sidecar_key not in harness.control_session.s3.objects
    assert set(harness.control_session.s3.objects) == {tracked_after.object_key}


@pytest.mark.parametrize(
    ("initial_mode", "replacement_mode"),
    ((DestinationMode.BEDROCK, DestinationMode.QUICK), (DestinationMode.QUICK, DestinationMode.BEDROCK)),
)
def test_reconciliation_between_managed_modes_replaces_sidecar_schema(
    monkeypatch: pytest.MonkeyPatch,
    initial_mode: DestinationMode,
    replacement_mode: DestinationMode,
) -> None:
    harness, configuration, target = _single_github_runtime(monkeypatch, initial_mode)
    assert harness.run(target, configuration) == 0
    tracked_before = next(iter(harness.client.state.values()))

    harness.client.mode = replacement_mode
    assert harness.run(target, configuration) == 0

    tracked_after = next(iter(harness.client.state.values()))
    assert tracked_after.sidecar_key == tracked_before.sidecar_key
    assert tracked_after.checksum != tracked_before.checksum
    assert tracked_after.sidecar_key is not None
    sidecar = json.loads(harness.control_session.s3.objects[tracked_after.sidecar_key])
    expected_key = "DocumentId" if replacement_mode is DestinationMode.QUICK else "metadataAttributes"
    assert expected_key in sidecar


def test_reconciliation_renderer_revision_alone_forces_write(monkeypatch: pytest.MonkeyPatch) -> None:
    harness, configuration, target = _single_github_runtime(monkeypatch, DestinationMode.QUICK)
    assert harness.run(target, configuration) == 0
    tracked_before = next(iter(harness.client.state.values()))
    mutations_before = _mutations(harness)

    monkeypatch.setattr(QuickKnowledgeBaseAdapter, "fingerprint", "quick:2")
    assert harness.run(target, configuration) == 0

    tracked_after = next(iter(harness.client.state.values()))
    assert tracked_after.object_key == tracked_before.object_key
    assert tracked_after.checksum != tracked_before.checksum
    assert _mutations(harness) == (
        mutations_before[0] + 2,
        mutations_before[1],
        mutations_before[2] + 1,
        mutations_before[3],
    )


def test_renderer_revision_sidecar_failure_preserves_tracked_body_and_retry_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, configuration, target = _single_github_runtime(monkeypatch, DestinationMode.QUICK)
    assert harness.run(target, configuration) == 0
    tracked_before = next(iter(harness.client.state.values()))
    assert tracked_before.sidecar_key is not None
    stored_body = harness.control_session.s3.objects[tracked_before.object_key]
    stored_sidecar = harness.control_session.s3.objects[tracked_before.sidecar_key]
    original_put = harness.control_session.s3.put_object
    failure_injected = False

    def fail_sidecar_once(**request: Any) -> None:
        nonlocal failure_injected
        if request["Key"] == tracked_before.sidecar_key and not failure_injected:
            failure_injected = True
            raise RuntimeError("sidecar write failed")
        original_put(**request)

    monkeypatch.setattr(harness.control_session.s3, "put_object", fail_sidecar_once)
    monkeypatch.setattr(QuickKnowledgeBaseAdapter, "fingerprint", "quick:2")

    with pytest.raises(SystemExit) as caught:
        harness.run(target, configuration)

    assert caught.value.code == 1
    assert harness.client.state[tracked_before.document_id] == tracked_before
    assert harness.control_session.s3.objects[tracked_before.object_key] == stored_body
    assert harness.control_session.s3.objects[tracked_before.sidecar_key] == stored_sidecar

    assert harness.run(target, configuration) == 0

    tracked_after = harness.client.state[tracked_before.document_id]
    assert tracked_after.object_key == tracked_before.object_key
    assert tracked_after.sidecar_key == tracked_before.sidecar_key
    assert tracked_after.checksum != tracked_before.checksum
    _assert_one_sidecar_per_document(harness)


def test_partial_source_failure_is_sanitized_before_destination_or_state_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = FakeRepository(101, "group/one", {"a.py": b"one", "failure.py": b"two"})
    original_get_contents = repository.get_contents

    def get_contents(path: str, *, ref: str) -> Any:
        if path == "failure.py":
            raise RuntimeError("private source failure")
        return original_get_contents(path, ref=ref)

    repository.get_contents = get_contents  # type: ignore[method-assign]
    monkeypatch.setattr(github_connector, "Github", lambda **_: FakeGithub({"group/one": repository}))
    harness = RuntimeHarness(monkeypatch, DestinationMode.S3, GitHubSourceSession("local-token"))
    configuration = GitHubConnectorConfiguration(
        token_secret_arn="arn:secret",
        repositories=(GitHubRepositoryConfiguration(full_name="group/one", file_extensions=(".py",)),),
    )

    with pytest.raises(SystemExit) as caught:
        harness.run("github_connector.connector:definition", configuration)

    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ccf-connector: connector run failed\n"
    assert harness.client.list_calls == 0
    assert harness.client.state == {}
    assert _mutations(harness) == (0, 0, 0, 0)


def test_github_partial_issue_enumeration_failure_has_no_mutation_and_static_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = FakeRepository(
        101,
        "private/repository",
        {},
        issues=FakePaginatedItems([github_entry(11, "Private issue", body="private body")], fail_after=1),
    )
    monkeypatch.setattr(github_connector, "Github", lambda **_: FakeGithub({"private/repository": repository}))
    harness = RuntimeHarness(monkeypatch, DestinationMode.BEDROCK, GitHubSourceSession("private-token"))
    configuration = GitHubConnectorConfiguration(
        token_secret_arn="arn:private-secret",
        repositories=(GitHubRepositoryConfiguration(full_name="private/repository"),),
    )

    with pytest.raises(SystemExit) as caught:
        harness.run("github_connector.connector:definition", configuration)

    assert caught.value.code == 1
    assert capsys.readouterr() == ("", "ccf-connector: connector run failed\n")
    assert harness.client.list_calls == 0
    assert harness.client.state == {}
    assert _mutations(harness) == (0, 0, 0, 0)


def test_github_partial_pull_request_enumeration_failure_has_no_mutation_and_static_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = FakeRepository(
        101,
        "private/repository",
        {},
        issues=FakePaginatedItems([github_entry(11, "Private issue")]),
        pull_requests=FakePaginatedItems([github_entry(21, "Private pull", body="private body")], fail_after=1),
    )
    monkeypatch.setattr(github_connector, "Github", lambda **_: FakeGithub({"private/repository": repository}))
    harness = RuntimeHarness(monkeypatch, DestinationMode.QUICK, GitHubSourceSession("private-token"))
    configuration = GitHubConnectorConfiguration(
        token_secret_arn="arn:private-secret",
        repositories=(GitHubRepositoryConfiguration(full_name="private/repository"),),
    )

    with pytest.raises(SystemExit) as caught:
        harness.run("github_connector.connector:definition", configuration)

    assert caught.value.code == 1
    assert capsys.readouterr() == ("", "ccf-connector: connector run failed\n")
    assert harness.client.list_calls == 0
    assert harness.client.state == {}
    assert _mutations(harness) == (0, 0, 0, 0)


def test_gitlab_partial_issue_enumeration_failure_has_no_mutation_and_static_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = FakeProject(
        1,
        "private/project",
        {},
        issues=FakeListEndpoint([gitlab_entry(11, "Private issue", description="private body")], fail_after=1),
    )
    monkeypatch.setattr(gitlab_connector.gitlab, "Gitlab", lambda **_: FakeGitlab({"private/project": project}))
    harness = RuntimeHarness(monkeypatch, DestinationMode.BEDROCK, GitLabSourceSession())
    configuration = GitLabConnectorConfiguration(
        token_secret_arn="arn:private-secret",
        projects=(GitLabProjectConfiguration(project_id="private/project"),),
    )

    with pytest.raises(SystemExit) as caught:
        harness.run("gitlab_connector.connector:definition", configuration)

    assert caught.value.code == 1
    assert capsys.readouterr() == ("", "ccf-connector: connector run failed\n")
    assert harness.client.list_calls == 0
    assert harness.client.state == {}
    assert _mutations(harness) == (0, 0, 0, 0)


def test_gitlab_partial_merge_request_enumeration_failure_has_no_mutation_and_static_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = FakeProject(
        1,
        "private/project",
        {},
        issues=FakeListEndpoint([gitlab_entry(11, "Private issue")]),
        merge_requests=FakeListEndpoint([gitlab_entry(21, "Private merge", description="private body")], fail_after=1),
    )
    monkeypatch.setattr(gitlab_connector.gitlab, "Gitlab", lambda **_: FakeGitlab({"private/project": project}))
    harness = RuntimeHarness(monkeypatch, DestinationMode.QUICK, GitLabSourceSession())
    configuration = GitLabConnectorConfiguration(
        token_secret_arn="arn:private-secret",
        projects=(GitLabProjectConfiguration(project_id="private/project"),),
    )

    with pytest.raises(SystemExit) as caught:
        harness.run("gitlab_connector.connector:definition", configuration)

    assert caught.value.code == 1
    assert capsys.readouterr() == ("", "ccf-connector: connector run failed\n")
    assert harness.client.list_calls == 0
    assert harness.client.state == {}
    assert _mutations(harness) == (0, 0, 0, 0)
