"""The connector's composition root, through the toolkit's testing module."""

import json
import re

import requests
from custom_connector_framework.testing import RunImplementationRequest, run_implementation
from pydantic import SecretStr
from requests.adapters import HTTPAdapter

from github_connector import GitHubClientsFactory, GitHubClientsRequest
from tests.configurations import make_configuration
from tests.unit.conftest import TOKEN, ConnectorRuns, failure_trace
from tests.unit.fake_github import FakeGithub, FakeRepository


def _runs() -> ConnectorRuns:
    return ConnectorRuns(FakeGithub([FakeRepository("aws-samples/demo", {"README.md": b"# Demo\n", "app.pyc": b"x"})]))


def test_the_github_client_is_built_with_the_token_from_the_secret() -> None:
    runs = _runs()

    runs.run(make_configuration())

    assert runs.clients.tokens == [TOKEN]


def test_the_connector_logs_through_the_logging_module() -> None:
    run = _runs().run(make_configuration())

    [completed] = [event for event in run.log_events if event.message == "owner_read_completed"]
    assert completed.logger == "activities.owner_read_activity"
    assert completed.level == "INFO"
    assert json.loads(completed.line)["repositories_read"] == 1


def test_a_custom_connector_without_a_secret_fails_naming_what_to_register() -> None:
    runs = _runs()

    run = run_implementation(
        RunImplementationRequest(
            implementation=runs.implementation(),
            connector_configuration=make_configuration().model_dump_json(),
        )
    )

    assert re.search(r"MissingConnectorInputError: .*secret_arn", failure_trace(run))


def test_an_unknown_configuration_field_fails_the_job() -> None:
    runs = _runs()

    run = run_implementation(
        RunImplementationRequest(
            implementation=runs.implementation(),
            connector_configuration=make_configuration().model_dump_json()[:-1] + ', "token_secret_arn": "x"}',
            secret=TOKEN,
        )
    )

    assert "extra" in failure_trace(run)


def test_a_custom_connector_without_a_connector_configuration_fails_naming_what_to_register() -> None:
    runs = _runs()

    run = run_implementation(RunImplementationRequest(implementation=runs.implementation(), secret=TOKEN))

    assert re.search(r"MissingConnectorInputError: .*connector_configuration", failure_trace(run))


def test_the_archive_client_jitters_its_retry_backoff() -> None:
    access = make_configuration().github.model_copy(update={"retry_attempts": 3, "retry_backoff_seconds": 2.0})

    clients = GitHubClientsFactory().create(GitHubClientsRequest(access=access, token=SecretStr(TOKEN)))

    session = clients.archive_session
    assert isinstance(session, requests.Session)
    adapter = session.get_adapter("https://api.github.com")
    assert isinstance(adapter, HTTPAdapter)
    assert adapter.max_retries.backoff_jitter == 2.0
