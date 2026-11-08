"""`ccf-connector run`: settings, the runtime role session's clients, class loading, and output on the real stdout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from ccf_api_models.operations import CcfApi
from pydantic import ValidationError

from custom_connector_framework import launcher
from custom_connector_framework.client import CcfClientSettings, RetrySettings, create_ccf_client
from custom_connector_framework.launcher import (
    ImplementationLoader,
    ImplementationLoadError,
    JobLauncher,
    RuntimeSettings,
)
from custom_connector_framework.testing import FakeSession, FakeSts
from tests.unit.runner.builders import CONNECTOR_ID, DESTINATION_ID, IDENTITY, JOB_ID, REGION, TENANT_ARN_PREFIX

TOOLKIT = Path(__file__).resolve().parents[2]
RUNTIME_ENVIRONMENT = {
    "CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT": "https://abcde12345.execute-api.us-east-1.amazonaws.com/v1",
    "AWS_REGION": REGION,
    "CUSTOM_CONNECTOR_FRAMEWORK_TENANT_ARN_PREFIX": TENANT_ARN_PREFIX,
    "CUSTOM_CONNECTOR_FRAMEWORK_DESTINATION_ID": DESTINATION_ID,
    "CUSTOM_CONNECTOR_FRAMEWORK_CUSTOM_CONNECTOR_ID": CONNECTOR_ID,
    "CUSTOM_CONNECTOR_FRAMEWORK_JOB_ID": JOB_ID,
}

NOISY_IMPLEMENTATION = textwrap.dedent(
    """
    import logging

    from custom_connector_framework import ConnectorImplementation


    class Noisy(ConnectorImplementation):
        def run(self) -> None:
            logging.getLogger().info("root logger line")
            logging.getLogger("urllib3.connectionpool").warning("third-party logger line")
            print("printed line")
            raise RuntimeError("sentinel-7f3a9c")
    """
)

DRIVER = textwrap.dedent(
    """
    import json
    import sys

    import botocore.client

    operations = []


    def record(client, operation_name, parameters):
        operations.append(operation_name)
        raise RuntimeError("no AWS call in this test")


    botocore.client.BaseClient._make_api_call = record

    from custom_connector_framework import launcher
    from tests.unit.runner.builders import RunnerWorld

    world = RunnerWorld()
    launcher.JobLauncher.runner = lambda self, settings: world.runner()
    code = launcher.main(["run", "noisy_implementation:Noisy"])
    sys.stderr.write(json.dumps({"operations": operations}))
    sys.exit(code)
    """
)


@pytest.fixture
def runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in RUNTIME_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def test_settings_read_the_six_job_definition_variables(runtime_environment: None) -> None:
    settings = RuntimeSettings()

    assert (settings.destination_id, settings.connector_id, settings.job_id) == (DESTINATION_ID, CONNECTOR_ID, JOB_ID)
    assert settings.tenant_arn_prefix == TENANT_ARN_PREFIX
    assert settings.region == REGION
    assert settings.api_endpoint == RUNTIME_ENVIRONMENT["CUSTOM_CONNECTOR_FRAMEWORK_API_ENDPOINT"]


def test_settings_give_the_jobs_identity(runtime_environment: None) -> None:
    assert RuntimeSettings().job_identity() == IDENTITY


def test_a_missing_variable_fails_naming_it(runtime_environment: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CUSTOM_CONNECTOR_FRAMEWORK_TENANT_ARN_PREFIX")

    with pytest.raises(ValidationError, match="CUSTOM_CONNECTOR_FRAMEWORK_TENANT_ARN_PREFIX"):
        RuntimeSettings()


def test_the_runtime_role_session_builds_only_the_sts_client(runtime_environment: None) -> None:
    runtime_session = FakeSession({"sts": FakeSts()}, region_name=REGION)

    JobLauncher(runtime_session).runner(RuntimeSettings())

    assert runtime_session.requested == ["sts"]


def test_the_jobs_client_retries_a_throttled_call_for_longer_than_a_tenant_script(
    runtime_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[CcfClientSettings] = []
    real = create_ccf_client

    def recording(settings: CcfClientSettings) -> CcfApi:
        built.append(settings)
        return real(settings)

    monkeypatch.setattr(launcher, "create_ccf_client", recording)

    JobLauncher(FakeSession({"sts": FakeSts()}, region_name=REGION)).runner(RuntimeSettings())

    [settings] = built
    assert settings.retry == launcher.JOB_RETRY_SETTINGS
    assert settings.retry.attempts > RetrySettings().attempts


@pytest.mark.parametrize("target", ["json", "json:loads", "json:Missing", "custom_connector_framework:JobContext"])
def test_a_target_that_isnt_a_connector_implementation_fails_to_load(target: str) -> None:
    with pytest.raises(ImplementationLoadError):
        ImplementationLoader().load(target)


def test_an_unloadable_target_exits_with_the_implementation_error_code(runtime_environment: None) -> None:
    assert launcher.main(["run", "json:loads"]) == 70


@pytest.fixture(scope="module")
def launched(tmp_path_factory: pytest.TempPathFactory) -> subprocess.CompletedProcess[str]:
    """One launcher run, in a subprocess, of an implementation that logs, prints, and raises."""
    directory = tmp_path_factory.mktemp("launcher")
    (directory / "noisy_implementation.py").write_text(NOISY_IMPLEMENTATION)
    (directory / "driver.py").write_text(DRIVER)
    environment = {**os.environ, **RUNTIME_ENVIRONMENT, "PYTHONPATH": os.pathsep.join([str(directory), str(TOOLKIT)])}
    return subprocess.run(  # noqa: S603 - runs this interpreter on a script the test wrote
        [sys.executable, str(directory / "driver.py")],
        capture_output=True,
        text=True,
        env=environment,
        cwd=TOOLKIT,
        check=False,
        timeout=60,
    )


def _records(completed: subprocess.CompletedProcess[str]) -> dict[str, dict[str, Any]]:
    lines = completed.stdout.splitlines()
    return {
        record["message"]: record for record in (json.loads(line) for line in lines if line.startswith('{"timestamp"'))
    }


def test_the_launcher_exits_with_the_implementation_error_code(launched: subprocess.CompletedProcess[str]) -> None:
    assert launched.returncode == 70


def test_a_printed_line_reaches_the_real_stdout(launched: subprocess.CompletedProcess[str]) -> None:
    assert "printed line" in launched.stdout.splitlines()


def test_root_and_third_party_logger_lines_are_json_with_their_logger_names(
    launched: subprocess.CompletedProcess[str],
) -> None:
    records = _records(launched)

    assert records["root logger line"]["logger"] == "root"
    assert records["third-party logger line"]["logger"] == "urllib3.connectionpool"


def test_every_json_line_carries_the_job_and_connector_ids(launched: subprocess.CompletedProcess[str]) -> None:
    records = _records(launched).values()

    assert {(record["job_id"], record["connector_id"]) for record in records} == {(JOB_ID, CONNECTOR_ID)}


def test_the_implementations_failure_is_logged_at_error_with_its_stack_trace(
    launched: subprocess.CompletedProcess[str],
) -> None:
    failure = _records(launched)["connector_implementation_failed"]

    assert failure["level"] == "ERROR"
    assert "Traceback" in failure["stack_trace"]
    assert "sentinel-7f3a9c" in failure["stack_trace"]


def test_the_launcher_calls_no_log_service(launched: subprocess.CompletedProcess[str]) -> None:
    operations = json.loads(launched.stderr.strip().splitlines()[-1])["operations"]

    assert "CreateLogStream" not in operations
    assert "PutLogEvents" not in operations


def test_a_module_that_raises_anything_at_import_fails_to_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "broken_implementation.py").write_text('raise RuntimeError("broken at import")\n')
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ImplementationLoadError):
        ImplementationLoader().load("broken_implementation:Anything")


def test_a_module_that_raises_at_import_exits_with_the_implementation_error_code(
    runtime_environment: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "broken_at_import.py").write_text('raise RuntimeError("broken at import")\n')
    monkeypatch.syspath_prepend(str(tmp_path))

    assert launcher.main(["run", "broken_at_import:Anything"]) == 70


def test_an_unexpected_settings_key_is_rejected(runtime_environment: None) -> None:
    with pytest.raises(ValidationError, match="unexpected"):
        RuntimeSettings(unexpected="value")


def test_a_malformed_container_variable_exits_with_the_implementation_error_code_naming_it(
    runtime_environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CUSTOM_CONNECTOR_FRAMEWORK_DESTINATION_ID", "not-a-destination-id")

    assert launcher.main(["run", "json:loads"]) == 70

    output = capsys.readouterr().out
    [invalid] = [line for line in map(json.loads, output.splitlines()) if line["message"] == "runtime_settings_invalid"]
    assert (invalid["level"], invalid["fields"]) == ("ERROR", ["CUSTOM_CONNECTOR_FRAMEWORK_DESTINATION_ID"])
    assert "not-a-destination-id" not in output


def test_env_example_lists_every_job_container_setting_with_a_valid_example(monkeypatch: pytest.MonkeyPatch) -> None:
    example = TOOLKIT.parents[1] / ".env.example"
    lines = example.read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if line.strip() and not line.startswith("#"))
    aliases = {str(field.validation_alias) for field in RuntimeSettings.model_fields.values()}

    assert aliases <= set(values)
    for name in aliases:
        monkeypatch.setenv(name, values[name])
    assert RuntimeSettings().job_id == values["CUSTOM_CONNECTOR_FRAMEWORK_JOB_ID"]
