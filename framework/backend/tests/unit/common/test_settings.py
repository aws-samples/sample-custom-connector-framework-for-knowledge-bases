"""Each composition root's settings read `.env.example` and fail the cold start naming a bad field."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from ccf_api_models.contracts import SettingsContract
from pydantic import BaseModel, ValidationError

from common.settings import (
    ApiSettings,
    HandlerSettings,
    IngestionConsumerSettings,
    ScheduledStartSettings,
)

ENV_EXAMPLE = Path(__file__).resolve().parents[5] / ".env.example"
EXAMPLE = dict(
    line.split("=", 1) for line in ENV_EXAMPLE.read_text().splitlines() if line.strip() and not line.startswith("#")
)
CONTRACT_ROOTS: Mapping[str, type[HandlerSettings]] = {
    "api_handler": ApiSettings,
    "scheduled_start_handler": ScheduledStartSettings,
    "ingestion_consumer_handler": IngestionConsumerSettings,
}


@pytest.fixture
def use_environment(monkeypatch: pytest.MonkeyPatch) -> Callable[[Mapping[str, str]], None]:
    """Return a function that replaces the settings' environment variables with the given values."""

    def replace(values: Mapping[str, str]) -> None:
        for name in EXAMPLE:
            monkeypatch.delenv(name, raising=False)
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return replace


def without(name: str) -> dict[str, str]:
    return {key: value for key, value in EXAMPLE.items() if key != name}


def failed_fields(error: ValidationError) -> set[str]:
    return {".".join(str(part) for part in detail["loc"]) for detail in error.errors()}


def environment_names(settings: type[BaseModel]) -> set[str]:
    """Return the environment names a settings model reads, joining a nested group and its member with `__`."""
    names: set[str] = set()
    for name, field in settings.model_fields.items():
        group = field.annotation
        if isinstance(group, type) and issubclass(group, BaseModel):
            names |= {f"{name}__{member}".upper() for member in group.model_fields}
        else:
            names.add(name.upper())
    return names


@pytest.mark.parametrize("settings", [ApiSettings, ScheduledStartSettings, IngestionConsumerSettings])
def test_each_root_reads_its_settings_from_the_env_example(
    use_environment: Callable[[Mapping[str, str]], None], settings: type[HandlerSettings]
) -> None:
    use_environment(EXAMPLE)

    settings()


@pytest.mark.parametrize(("handler", "settings"), CONTRACT_ROOTS.items())
def test_each_roots_environment_names_equal_its_settings_contract_entry(
    handler: str, settings: type[HandlerSettings]
) -> None:
    assert environment_names(settings) - {"AWS_REGION"} == SettingsContract.load().handlers[handler]


def test_api_settings_split_the_tenant_accounts_and_parse_the_routes(
    use_environment: Callable[[Mapping[str, str]], None],
) -> None:
    use_environment({**EXAMPLE, "ALLOWED_TENANT_ACCOUNT_IDS": "111122223333, 444455556666"})

    settings = ApiSettings()

    assert settings.allowed_tenant_account_ids == ("111122223333", "444455556666")
    assert [route.method for route in settings.runtime_roles.routes] == ["GET"]


def test_an_empty_tenant_account_list_allows_only_the_service_account(
    use_environment: Callable[[Mapping[str, str]], None],
) -> None:
    use_environment({**EXAMPLE, "ALLOWED_TENANT_ACCOUNT_IDS": ""})

    assert ApiSettings().allowed_tenant_account_ids == ()


@pytest.mark.parametrize("routes", ["not json", "[]", '[{"method":"GET"}]', '[{"method":"get","path":"/x"}]'])
def test_malformed_runtime_routes_fail_the_api_settings(
    use_environment: Callable[[Mapping[str, str]], None], routes: str
) -> None:
    use_environment({**EXAMPLE, "RUNTIME_ROLES__ROUTES": routes})

    with pytest.raises(ValidationError) as raised:
        ApiSettings()

    assert all(field.startswith("runtime_roles.routes") for field in failed_fields(raised.value))


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("WORKFLOWS__DESTINATION", "workflows.destination"),
        ("TABLES__DESTINATIONS", "tables.destinations"),
        ("AWS_URL_SUFFIX", "aws_url_suffix"),
        ("SCHEDULER__RETRY_ATTEMPTS", "scheduler.retry_attempts"),
    ],
)
def test_a_missing_api_setting_fails_naming_the_field(
    use_environment: Callable[[Mapping[str, str]], None], name: str, field: str
) -> None:
    use_environment(without(name))

    with pytest.raises(ValidationError) as raised:
        ApiSettings()

    assert failed_fields(raised.value) == {field}


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("RUNTIME_ROLES__ROLE_PATH", "runtime_roles.role_path"),
        ("RUNTIME_ROLES__ROLE_NAME_PREFIX", "runtime_roles.role_name_prefix"),
        ("RUNTIME_ROLES__POLICY_NAME", "runtime_roles.policy_name"),
        ("RUNTIME_ROLES__MAX_SESSION_SECONDS", "runtime_roles.max_session_seconds"),
    ],
)
def test_a_missing_runtime_role_name_or_session_fails_naming_the_field(
    use_environment: Callable[[Mapping[str, str]], None], name: str, field: str
) -> None:
    use_environment(without(name))

    with pytest.raises(ValidationError) as raised:
        ApiSettings()

    assert failed_fields(raised.value) == {field}


@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("RUNTIME_ROLES__ROLE_PATH", "ccf-runtime", "runtime_roles.role_path"),
        ("RUNTIME_ROLES__ROLE_NAME_PREFIX", "Ccf Runtime-", "runtime_roles.role_name_prefix"),
        ("RUNTIME_ROLES__POLICY_NAME", "", "runtime_roles.policy_name"),
        ("RUNTIME_ROLES__MAX_SESSION_SECONDS", "3599", "runtime_roles.max_session_seconds"),
        ("RUNTIME_ROLES__MAX_SESSION_SECONDS", "43201", "runtime_roles.max_session_seconds"),
    ],
)
def test_a_malformed_or_out_of_range_runtime_role_setting_fails_naming_the_field(
    use_environment: Callable[[Mapping[str, str]], None], name: str, value: str, field: str
) -> None:
    use_environment({**EXAMPLE, name: value})

    with pytest.raises(ValidationError) as raised:
        ApiSettings()

    assert failed_fields(raised.value) == {field}


JOB_FIELDS = [
    ("JOBS__JOB_EXECUTION_ARN", "jobs.job_execution_arn"),
    ("JOBS__RECORD_RETENTION_SECONDS", "jobs.record_retention_seconds"),
    ("JOBS__ACTIVE_RECORD_RETENTION_SECONDS", "jobs.active_record_retention_seconds"),
]
JOB_DEFINITION_FIELDS = [
    ("JOB_DEFINITIONS__NAME_PREFIX", "job_definitions.name_prefix"),
    ("JOB_DEFINITIONS__EXECUTION_ROLE_ARN", "job_definitions.execution_role_arn"),
    ("JOB_DEFINITIONS__API_ENDPOINT", "job_definitions.api_endpoint"),
    ("JOB_DEFINITIONS__LOG_ROUTER_IMAGE", "job_definitions.log_router_image"),
    ("JOB_DEFINITIONS__LOG_ROUTER_LOG_GROUP", "job_definitions.log_router_log_group"),
    ("JOB_DEFINITIONS__LOG_ROUTER_STREAM_PREFIX", "job_definitions.log_router_stream_prefix"),
]


@pytest.mark.parametrize(("name", "field"), JOB_FIELDS + JOB_DEFINITION_FIELDS)
def test_a_missing_job_setting_fails_the_api_naming_the_field(
    use_environment: Callable[[Mapping[str, str]], None], name: str, field: str
) -> None:
    use_environment(without(name))

    with pytest.raises(ValidationError) as raised:
        ApiSettings()

    assert failed_fields(raised.value) == {field}


@pytest.mark.parametrize(("name", "field"), JOB_FIELDS)
def test_a_missing_job_setting_fails_the_scheduled_start_lambda_naming_the_field(
    use_environment: Callable[[Mapping[str, str]], None], name: str, field: str
) -> None:
    use_environment(without(name))

    with pytest.raises(ValidationError) as raised:
        ScheduledStartSettings()

    assert failed_fields(raised.value) == {field}


def test_a_log_router_image_without_a_version_tag_is_rejected(
    use_environment: Callable[[Mapping[str, str]], None],
) -> None:
    use_environment(
        {**EXAMPLE, "JOB_DEFINITIONS__LOG_ROUTER_IMAGE": "public.ecr.aws/aws-observability/aws-for-fluent-bit"}
    )

    with pytest.raises(ValidationError) as raised:
        ApiSettings()

    assert failed_fields(raised.value) == {"job_definitions.log_router_image"}


def test_a_missing_destination_workflow_fails_the_ingestion_consumer(
    use_environment: Callable[[Mapping[str, str]], None],
) -> None:
    use_environment(without("WORKFLOWS__DESTINATION"))

    with pytest.raises(ValidationError) as raised:
        IngestionConsumerSettings()

    assert failed_fields(raised.value) == {"workflows.destination"}


def test_a_missing_idempotency_expiry_fails_the_scheduled_start_lambda(
    use_environment: Callable[[Mapping[str, str]], None],
) -> None:
    use_environment(without("IDEMPOTENCY__EXPIRY_SECONDS"))

    with pytest.raises(ValidationError) as raised:
        ScheduledStartSettings()

    assert failed_fields(raised.value) == {"idempotency"}
