"""One owner's reads across jobs, through the toolkit's testing module."""

import json
from datetime import timedelta
from typing import Any

from ccf_api_models.identifiers import CollectionIdentity
from custom_connector_framework.testing import SeedCollection, SeedState, TestRun

from common.configuration import GitHubConnectorConfiguration
from tests.configurations import make_configuration
from tests.unit.conftest import FIXED_NOW, ConnectorRuns, failure_trace
from tests.unit.fake_github import FakeGithub, FakeRepository

_METADATA_SUFFIX = ".metadata.json"
PATTERNS_ID = 1001
WORKSHOP_ID = 1002
SAMPLES_ID = 1003


def _repositories() -> list[FakeRepository]:
    return [
        FakeRepository(
            "aws-samples/serverless-patterns",
            {
                "README.md": b"# Patterns\n",
                "app/handler.py": b"def handler(event, context):\n    return event\n",
                "Dockerfile": b"FROM python:3.14-slim\n",
                "node_modules/left-pad/index.js": b"module.exports = 1\n",
            },
            pushed_at="2026-10-02T00:00:00Z",
            github_id=PATTERNS_ID,
        ),
        FakeRepository("aws-samples/cdk-workshop", {"README.md": b"# Workshop\n"}, github_id=WORKSHOP_ID),
    ]


def _runs() -> ConnectorRuns:
    return ConnectorRuns(FakeGithub(_repositories()))


def _document_ids(run: TestRun) -> set[str]:
    return {document.document_id for document in run.documents}


def _metadata(run: TestRun) -> dict[str, dict[str, Any]]:
    """Each stored document's metadata attributes, by its `repository/path`."""
    attributes = (
        json.loads(body)["metadataAttributes"] for key, body in run.files.items() if key.endswith(_METADATA_SUFFIX)
    )
    return {
        f"{values['repository']['value']['stringValue']}/{values['path']['value']['stringValue']}": values
        for values in attributes
    }


def test_a_first_read_stores_every_kept_file_under_its_repository_id_and_path() -> None:
    run = _runs().run(make_configuration())

    assert _document_ids(run) == {
        f"{PATTERNS_ID}/README.md",
        f"{PATTERNS_ID}/app/handler.py",
        f"{PATTERNS_ID}/Dockerfile",
        f"{WORKSHOP_ID}/README.md",
    }


def test_each_document_is_in_its_repositorys_collection() -> None:
    run = _runs().run(make_configuration())

    assert {document.collection_id for document in run.documents} == {str(PATTERNS_ID), str(WORKSHOP_ID)}


def test_each_repository_is_a_collection_named_by_its_numeric_id_beside_the_default_one() -> None:
    run = _runs().run(make_configuration())

    assert {collection.collection_id for collection in run.collections} == {None, str(PATTERNS_ID), str(WORKSHOP_ID)}


def test_a_second_run_reads_every_checkpoint_from_one_collections_listing() -> None:
    repositories = [
        *_repositories(),
        FakeRepository("aws-samples/aws-samples-guide", {"README.md": b"# Guide\n"}, github_id=SAMPLES_ID),
    ]
    runs = ConnectorRuns(FakeGithub(repositories))
    three = make_configuration().model_copy(update={"max_repositories": 3})
    runs.run(three)

    second = runs.run(three)

    assert second.api_calls.count("ListCustomConnectorCollections") == 1
    assert "GetCustomConnectorCheckpoint" not in second.api_calls
    assert second.report.collection_counts.unchanged == 4


def test_every_document_carries_the_attributes_search_filters_on_and_its_github_url() -> None:
    run = _runs().run(make_configuration())

    attributes = _metadata(run)["aws-samples/cdk-workshop/README.md"]

    assert attributes["owner"]["value"]["stringValue"] == "aws-samples"
    assert "org" not in attributes
    assert attributes["repository_id"]["value"]["stringValue"].startswith("repo_")
    assert attributes["repo_created_at"] == {"value": {"type": "NUMBER", "numberValue": 1714979289}}
    assert (
        attributes["source_uri"]["value"]["stringValue"]
        == "https://github.com/aws-samples/cdk-workshop/blob/main/README.md"
    )


def test_each_document_version_is_its_git_blob_sha() -> None:
    run = _runs().run(make_configuration())

    [readme] = [document for document in run.documents if document.document_id == f"{WORKSHOP_ID}/README.md"]
    # `printf '# Workshop\n' | git hash-object --stdin`
    assert readme.source_version == "e58e637ca30537d0c61bfcc33f49eea907b4347c"


def test_an_unchanged_repository_is_not_downloaded_and_keeps_its_documents() -> None:
    runs = _runs()
    first = runs.run(make_configuration())
    downloads = list(runs.github.downloads)

    second = runs.run(make_configuration())

    assert runs.github.downloads == downloads
    assert _document_ids(second) == _document_ids(first)
    assert second.files == {}


def test_a_repository_checkpoint_stores_its_last_push_in_utc_and_a_second_run_skips_it() -> None:
    runs = _runs()
    runs.run(make_configuration())
    [stored] = [collection for collection in runs.seed.collections if collection.collection_id == str(PATTERNS_ID)]

    second = runs.run(make_configuration())

    assert stored.checkpoint is not None
    assert json.loads(stored.checkpoint)["pushed_at"] in ("2026-10-02T00:00:00Z", "2026-10-02T00:00:00+00:00")
    assert runs.github.downloads.count("aws-samples/serverless-patterns") == 1
    assert {event.reason for event in second.log_events if event.message == "repository_skipped"} == {"unchanged"}


def test_a_pushed_repository_records_only_its_changed_files_and_deletes_a_removed_one() -> None:
    runs = _runs()
    first = runs.run(make_configuration())
    patterns = runs.github.repositories["aws-samples/serverless-patterns"]
    del patterns.files["Dockerfile"]
    patterns.files["README.md"] = b"# Patterns, updated\n"
    patterns.pushed_at = "2026-10-03T00:00:00Z"

    second = runs.run(make_configuration())

    hashes = {document.document_id: document.content_hash for document in first.documents}
    changed = {
        document.document_id for document in second.documents if hashes[document.document_id] != document.content_hash
    }
    assert changed == {f"{PATTERNS_ID}/README.md"}
    assert f"{PATTERNS_ID}/Dockerfile" not in _document_ids(second)
    assert second.report.total.deleted == 1


def test_an_unchanged_repository_logs_repository_skipped_with_its_reason() -> None:
    runs = _runs()
    runs.run(make_configuration())

    second = runs.run(make_configuration())

    skipped = [event for event in second.log_events if event.message == "repository_skipped"]
    assert {(json.loads(event.line)["repository"], event.reason) for event in skipped} == {
        ("aws-samples/serverless-patterns", "unchanged"),
        ("aws-samples/cdk-workshop", "unchanged"),
    }


def test_a_repository_that_drops_out_of_the_selection_loses_its_collection_and_the_default_one_stays() -> None:
    runs = _runs()
    runs.run(make_configuration())

    second = runs.run(make_configuration().model_copy(update={"max_repositories": 1}))

    assert {collection.collection_id for collection in second.collections} == {None, str(PATTERNS_ID)}
    assert {document.collection_id for document in second.documents} == {str(PATTERNS_ID)}


def test_a_repository_github_no_longer_has_loses_its_collection() -> None:
    runs = _runs()
    runs.run(make_configuration())
    workshop = runs.github.repositories["aws-samples/cdk-workshop"]
    workshop.archive_missing = True
    workshop.pushed_at = "2026-10-04T00:00:00Z"

    second = runs.run(make_configuration())

    assert str(WORKSHOP_ID) not in {collection.collection_id for collection in second.collections}


def test_a_repository_github_no_longer_has_logs_repository_dropped() -> None:
    runs = _runs()
    runs.run(make_configuration())
    workshop = runs.github.repositories["aws-samples/cdk-workshop"]
    workshop.archive_missing = True
    workshop.pushed_at = "2026-10-04T00:00:00Z"

    second = runs.run(make_configuration())

    dropped = [event for event in second.log_events if event.message == "repository_dropped"]
    assert [(event.level, json.loads(event.line)["repository"], event.reason) for event in dropped] == [
        ("WARNING", "aws-samples/cdk-workshop", "not_found")
    ]


def test_a_full_read_runs_every_repository_again_after_the_interval() -> None:
    runs = _runs()
    runs.run(make_configuration())
    runs.clock.current = FIXED_NOW + timedelta(days=28)

    runs.run(make_configuration())

    assert runs.github.downloads.count("aws-samples/cdk-workshop") == 2
    [default] = [collection for collection in runs.seed.collections if collection.collection_id is None]
    assert default.checkpoint is not None
    assert json.loads(default.checkpoint)["last_full_read_at"].startswith("2026-11-27")


def test_no_full_read_runs_before_the_interval() -> None:
    runs = _runs()
    runs.run(make_configuration())
    runs.clock.current = FIXED_NOW + timedelta(days=27)

    runs.run(make_configuration())

    assert runs.github.downloads.count("aws-samples/cdk-workshop") == 1


def test_a_content_filter_change_reads_every_repository_again() -> None:
    runs = _runs()
    runs.run(make_configuration())

    second = runs.run(
        make_configuration().model_copy(update={"excluded_path_patterns": ("node_modules/", "Dockerfile")})
    )

    assert f"{PATTERNS_ID}/Dockerfile" not in _document_ids(second)


def test_a_github_server_error_after_retries_fails_the_job_and_deletes_nothing() -> None:
    runs = _runs()
    runs.run(make_configuration())
    runs.github.server_error = True

    assert "GithubException" in failure_trace(runs.run(make_configuration()))

    report = runs.stores[-1].get_report()
    assert report.total.deleted == 0
    assert report.collection_counts.deleted == 0


def test_a_broken_archive_download_is_read_again() -> None:
    repositories = _repositories()
    repositories[1].broken_downloads = 1
    runs = ConnectorRuns(FakeGithub(repositories))
    retrying = make_configuration()
    retrying = retrying.model_copy(update={"github": retrying.github.model_copy(update={"retry_attempts": 1})})

    run = runs.run(retrying)

    assert runs.github.downloads.count("aws-samples/cdk-workshop") == 2
    assert f"{WORKSHOP_ID}/README.md" in _document_ids(run)


def _retrying(attempts: int, backoff_seconds: float) -> GitHubConnectorConfiguration:
    configuration = make_configuration()
    return configuration.model_copy(
        update={
            "github": configuration.github.model_copy(
                update={"retry_attempts": attempts, "retry_backoff_seconds": backoff_seconds}
            )
        }
    )


def test_each_archive_re_read_waits_up_to_the_doubling_backoff() -> None:
    repositories = _repositories()
    repositories[1].broken_downloads = 2
    runs = ConnectorRuns(FakeGithub(repositories))

    runs.run(_retrying(attempts=2, backoff_seconds=1.5))

    assert runs.sleeper.ceilings == [1.5, 3.0]


def test_an_archive_that_stays_broken_after_every_attempt_fails_the_job_and_deletes_nothing() -> None:
    runs = _runs()
    runs.run(make_configuration())
    workshop = runs.github.repositories["aws-samples/cdk-workshop"]
    workshop.broken_downloads = 2
    workshop.pushed_at = "2026-10-04T00:00:00Z"

    assert "ArchiveDownloadBrokenError" in failure_trace(runs.run(_retrying(attempts=1, backoff_seconds=0.0)))

    report = runs.stores[-1].get_report()
    assert report.total.deleted == 0
    assert report.collection_counts.deleted == 0


def test_an_archive_github_refuses_fails_the_job_and_deletes_nothing() -> None:
    runs = _runs()
    runs.run(make_configuration())
    runs.github.forbidden_repository = "aws-samples/cdk-workshop"
    runs.github.repositories["aws-samples/cdk-workshop"].pushed_at = "2026-10-04T00:00:00Z"

    assert "GithubException" in failure_trace(runs.run(make_configuration()))

    report = runs.stores[-1].get_report()
    assert report.total.deleted == 0
    assert report.collection_counts.deleted == 0


def _with_checkpoint(runs: ConnectorRuns, collection_id: str | None) -> None:
    """Replace one stored collection's checkpoint with text that isn't a checkpoint."""
    runs.seed = SeedState(
        documents=runs.seed.documents,
        collections=tuple(
            SeedCollection(
                collection_id=collection.collection_id,
                checkpoint="not a checkpoint" if collection.collection_id == collection_id else collection.checkpoint,
            )
            for collection in runs.seed.collections
        ),
    )


def test_an_unreadable_source_checkpoint_is_logged_and_runs_a_full_read() -> None:
    runs = _runs()
    runs.run(make_configuration())
    downloads = len(runs.github.downloads)
    _with_checkpoint(runs, None)

    second = runs.run(make_configuration())

    assert len(runs.github.downloads) == downloads + 2
    unreadable = [event for event in second.log_events if event.message == "checkpoint_unreadable"]
    assert [(event.level, event.collection_id) for event in unreadable] == [("WARNING", "col-default")]
    assert "not a checkpoint" not in str(second.log_events)


def test_an_unreadable_repository_checkpoint_is_logged_and_reads_the_repository() -> None:
    runs = _runs()
    runs.run(make_configuration())
    _with_checkpoint(runs, str(WORKSHOP_ID))

    second = runs.run(make_configuration())

    assert runs.github.downloads.count("aws-samples/cdk-workshop") == 2
    assert runs.github.downloads.count("aws-samples/serverless-patterns") == 1
    unreadable = [event for event in second.log_events if event.message == "checkpoint_unreadable"]
    assert [(event.level, event.collection_id) for event in unreadable] == [
        ("WARNING", CollectionIdentity.of(str(WORKSHOP_ID)).collection_id)
    ]
