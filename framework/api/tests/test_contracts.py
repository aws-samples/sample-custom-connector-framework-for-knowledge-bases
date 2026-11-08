"""The packaged contract files load into their models, and a key the model doesn't declare fails."""

import json
from importlib.resources import files

import pytest
from ccf_api_models.contracts import (
    Contract,
    IdentifierExamples,
    JobContainerContract,
    MetricsContract,
    RuntimeRoleTagsContract,
    SettingsContract,
    TablesContract,
)
from ccf_api_models.job_container import JobExitCode, RuntimeVariable
from pydantic import ValidationError


def test_tables_contract_names_the_six_tables() -> None:
    tables = TablesContract.load()

    assert {table["name"] for table in tables.model_dump().values()} == {
        "CcfDestinations",
        "CcfCustomConnectors",
        "CcfCustomConnectorJobs",
        "CcfCollections",
        "CcfDocuments",
        "CcfIdempotency",
    }


def test_every_metric_dimension_set_uses_declared_dimensions() -> None:
    metrics = MetricsContract.load()

    used = {dimension for metric in metrics.metrics.values() for group in metric.dimension_sets for dimension in group}

    assert used <= metrics.dimensions.keys()


def test_settings_contract_lists_each_handler() -> None:
    assert SettingsContract.load().handlers.keys() == {
        "api_handler",
        "scheduled_start_handler",
        "ingestion_consumer_handler",
    }


def test_runtime_role_tags_contract_holds_the_five_tag_keys() -> None:
    tags = RuntimeRoleTagsContract.load()

    assert (tags.destination_id, tags.connector_id, tags.tenant_account) == (
        "ccf:destination-id",
        "ccf:connector-id",
        "ccf:tenant-account",
    )
    assert (tags.source_access_role, tags.log_delivery_role) == ("ccf:source-access-role", "ccf:log-delivery-role")


@pytest.mark.parametrize(
    "contract",
    [
        TablesContract,
        MetricsContract,
        SettingsContract,
        IdentifierExamples,
        RuntimeRoleTagsContract,
        JobContainerContract,
    ],
)
def test_a_contract_with_an_unknown_key_fails(contract: type[Contract]) -> None:
    document = json.loads(files("ccf_api_models.contracts").joinpath(contract.file_name).read_text(encoding="utf-8"))
    document["unexpected"] = True

    with pytest.raises(ValidationError, match="unexpected"):
        contract.model_validate_json(json.dumps(document))


def test_the_job_container_enums_hold_every_contract_variable_and_exit_code() -> None:
    contract = JobContainerContract.load()

    assert {variable.value for variable in RuntimeVariable} == set(contract.variables.values())
    assert {code.name: code.value for code in JobExitCode} == dict(contract.exit_codes)
