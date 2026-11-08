"""The binder builds the generated request model from every HTTP source and rejects what can't bind."""

from __future__ import annotations

from typing import Any

import pytest
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.models import GetDestinationRequest, ListCustomConnectorJobsRequest, ListDestinationsRequest
from ccf_api_models.operations import Operation

from common.errors import BadRequestError
from common.request_binder import RequestBinder

CATALOG = OperationCatalog.load()
DESTINATION_ID = "dst-7c41e9a2b5d0"


def binder(event: dict[str, Any]) -> RequestBinder:
    app = APIGatewayRestResolver()
    app.current_event = APIGatewayProxyEvent(event)
    return RequestBinder(app)


def event(**members: Any) -> dict[str, Any]:
    return {
        "pathParameters": None,
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "headers": None,
        "multiValueHeaders": None,
        "body": None,
        **members,
    }


def test_bind_reads_path_header_and_query_members_converted_to_their_types() -> None:
    bound = binder(
        event(
            queryStringParameters={"max_results": "5", "name": "docs"},
            multiValueQueryStringParameters={"max_results": ["5"], "name": ["docs"]},
            headers={"X-Correlation-Id": "c-1"},
        )
    ).bind(CATALOG.spec(Operation.LIST_DESTINATIONS))

    assert bound == ListDestinationsRequest(max_results=5, name="docs", correlation_id="c-1")


def test_bind_reads_a_header_member_of_get_destination() -> None:
    bound = binder(
        event(pathParameters={"destination_id": DESTINATION_ID}, headers={"x-ccf-connector-id": "cc-e020d65d76c0"})
    ).bind(CATALOG.spec(Operation.GET_DESTINATION))

    assert bound == GetDestinationRequest(destination_id=DESTINATION_ID, connector_id="cc-e020d65d76c0")


def test_a_list_without_max_results_gets_the_model_default() -> None:
    bound = binder(event()).bind(CATALOG.spec(Operation.LIST_DESTINATIONS))

    assert isinstance(bound, ListDestinationsRequest)
    assert bound.max_results == 100


@pytest.mark.parametrize(
    ("members", "fields"),
    [
        (
            {"pathParameters": {"destination_id": DESTINATION_ID}, "body": '{"destination_id": "dst-0a1b2c3d4e5f"}'},
            ("destination_id",),
        ),
        ({"multiValueQueryStringParameters": {"name": ["a", "b"]}}, ("name",)),
        ({"queryStringParameters": {"color": "red"}}, ("color",)),
        ({"multiValueHeaders": {"x-correlation-id": ["c-1", "c-2"]}}, ("x-correlation-id",)),
        ({"queryStringParameters": {"max_results": "five"}}, ("max_results",)),
        ({"queryStringParameters": {"max_results": "500"}}, ("max_results",)),
    ],
    ids=["two sources", "repeated query", "unmodeled query", "repeated header", "non-integer", "model violation"],
)
def test_bind_rejects_what_cant_bind_naming_the_members(members: dict[str, Any], fields: tuple[str, ...]) -> None:
    with pytest.raises(BadRequestError) as raised:
        binder(event(**members)).bind(CATALOG.spec(Operation.LIST_DESTINATIONS))

    assert raised.value.fields == fields


def test_a_body_that_isnt_a_json_object_is_rejected() -> None:
    with pytest.raises(BadRequestError):
        binder(event(body="[1, 2]")).bind(CATALOG.spec(Operation.CREATE_DESTINATION))


def test_a_status_filter_binds_as_the_enum() -> None:
    bound = binder(
        event(
            pathParameters={"destination_id": DESTINATION_ID, "connector_id": "cc-e020d65d76c0"},
            queryStringParameters={"status": "RUNNING"},
        )
    ).bind(CATALOG.spec(Operation.LIST_CUSTOM_CONNECTOR_JOBS))

    assert isinstance(bound, ListCustomConnectorJobsRequest)
    assert bound.status is not None and bound.status.value == "RUNNING"
