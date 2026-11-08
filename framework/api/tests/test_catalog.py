"""The catalog read from the packaged `openapi.json` matches the Smithy model's operations."""

import json
from importlib.resources import files
from pathlib import Path

import ccf_api_models.catalog as catalog_module
import pytest
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.models import GetDestinationRequest
from ccf_api_models.operations import Operation

CATALOG = OperationCatalog.load()


def test_every_operation_has_a_spec() -> None:
    assert {spec.operation for spec in CATALOG.specs()} == set(Operation)


def test_a_request_type_finds_its_operations_spec() -> None:
    assert CATALOG.spec_for(GetDestinationRequest) == CATALOG.spec(Operation.GET_DESTINATION)


def test_only_the_three_creates_take_a_client_token() -> None:
    assert {spec.operation: spec.client_token_member for spec in CATALOG.specs() if spec.client_token_member} == {
        Operation.CREATE_DESTINATION: "client_token",
        Operation.CREATE_CUSTOM_CONNECTOR: "client_token",
        Operation.START_CUSTOM_CONNECTOR_JOB: "client_token",
    }


def test_the_five_lists_page_and_name_their_filter() -> None:
    assert {
        spec.operation: (spec.pagination.items, spec.filter_member)
        for spec in CATALOG.specs()
        if spec.pagination is not None
    } == {
        Operation.LIST_DESTINATIONS: ("destinations", "name"),
        Operation.LIST_CUSTOM_CONNECTORS: ("connectors", "name"),
        Operation.LIST_CUSTOM_CONNECTOR_JOBS: ("jobs", "status"),
        Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS: ("documents", "collection_id"),
        Operation.LIST_CUSTOM_CONNECTOR_COLLECTIONS: ("collections", "collection_id"),
    }


def test_an_operation_without_a_caller_header_fails_the_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = json.loads(files("ccf_api_models").joinpath("openapi.json").read_text(encoding="utf-8"))
    operation = document["paths"]["/api/v1/destinations/{destination_id}"]["get"]
    operation["parameters"] = [item for item in operation["parameters"] if item["name"] != "x-correlation-id"]
    (tmp_path / "openapi.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(catalog_module, "files", lambda _package: tmp_path)

    with pytest.raises(ValueError, match="GetDestination lacks a CallerHeaders member: \\['correlation_id'\\]"):
        OperationCatalog.load()
