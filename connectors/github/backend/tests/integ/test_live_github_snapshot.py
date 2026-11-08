"""A read of live GitHub through the real connector and the toolkit's testing module.

GitHub is real; the framework API and S3 are the testing module's in-memory fakes. The test reads
a GitHub token from the `GITHUB_TOKEN` environment variable and is skipped without one. Run it with
`GITHUB_TOKEN=<token> uv run pytest tests/integ` from this directory.
"""

import json
import os
from pathlib import PurePosixPath

import pytest
from custom_connector_framework.store import BedrockFormat
from custom_connector_framework.testing import RunImplementationRequest, TestRun, run_implementation

from common.configuration import GitHubConnectorConfiguration
from github_connector import GitHubConnectorImplementation
from tests.configurations import make_configuration

TOKEN = os.environ.get("GITHUB_TOKEN", "")
METADATA_SUFFIX = ".metadata.json"
MAX_REPOSITORIES = 2

pytestmark = pytest.mark.skipif(not TOKEN, reason="GITHUB_TOKEN holds no GitHub token")


def _live_configuration() -> GitHubConnectorConfiguration:
    configuration = make_configuration()
    return configuration.model_copy(
        update={
            "max_repositories": MAX_REPOSITORIES,
            "max_file_bytes": 104_857_600,
            "github": configuration.github.model_copy(
                update={
                    "request_interval_seconds": 1.0,
                    "request_timeout_seconds": 60,
                    "retry_attempts": 3,
                    "retry_backoff_seconds": 2.0,
                    "max_rate_limit_wait_seconds": 120.0,
                }
            ),
        }
    )


@pytest.fixture(scope="module")
def live_run() -> TestRun:
    return run_implementation(
        RunImplementationRequest(
            implementation=GitHubConnectorImplementation,
            connector_configuration=_live_configuration().model_dump_json(),
            secret=TOKEN,
        )
    )


def test_a_live_read_stores_documents_from_the_selected_repositories(live_run: TestRun) -> None:
    collections = {document.collection_id for document in live_run.documents}

    assert 0 < len(collections) <= MAX_REPOSITORIES


def test_every_live_document_carries_its_repository_id(live_run: TestRun) -> None:
    for key, body in live_run.files.items():
        if key.endswith(METADATA_SUFFIX):
            attributes = json.loads(body)["metadataAttributes"]
            assert attributes["repository_id"]["value"]["stringValue"].startswith("repo_")


def test_live_documents_are_stored_under_names_bedrock_ingests(live_run: TestRun) -> None:
    stored = {key for key in live_run.files if not key.endswith(METADATA_SUFFIX)}

    assert all(BedrockFormat.SUPPORTED_TYPES.supports(PurePosixPath(key).suffix.lower().lstrip(".")) for key in stored)
