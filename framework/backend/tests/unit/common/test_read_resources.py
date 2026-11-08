"""The read-only Get and List classes read their members from the operation's spec."""

from __future__ import annotations

from typing import Any

import pytest
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, Partition, TenantContext
from ccf_api_models.models import (
    Destination,
    DestinationResources,
    DestinationStatus,
    DestinationSummary,
    DestinationType,
    GetDestinationRequest,
    JobStatus,
    JobSummary,
    ListCustomConnectorJobsRequest,
    ListDestinationsRequest,
)
from ccf_api_models.operations import Operation

from common.errors import ResourceNotFoundError
from common.operation import OperationRequest, RequestTrace
from common.read_resources import ListResources, ReadResource
from common.storage.ddb.model_table import ListItemsRequest, Page

CATALOG = OperationCatalog.load()
TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
TRACE = RequestTrace(correlation_id="c-1", api_request_id="r-1")
DESTINATION = Destination(
    destination_id="dst-7c41e9a2b5d0",
    arn="arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0",
    name="docs",
    type=DestinationType.S3,
    access_control_enabled=False,
    status=DestinationStatus.ACTIVE,
    role_arn="arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-docs",
    destination_resources=DestinationResources(),
    created_at=1.0,
    updated_at=1.0,
)
SUMMARY = DestinationSummary.model_validate(DESTINATION.model_dump(include=set(DestinationSummary.model_fields)))


def request(payload: Any, connector: Any = None) -> OperationRequest[Any]:
    """`model_construct`, so a test can stand in any object for the resolver's custom connector item."""
    return OperationRequest.model_construct(tenant=TENANT, payload=payload, trace=TRACE, connector=connector)


class Recorder:
    def __init__(self, page: Page[Any]) -> None:
        self.page = page
        self.requests: list[ListItemsRequest[Any]] = []

    def __call__(self, list_request: ListItemsRequest[Any]) -> Page[Any]:
        self.requests.append(list_request)
        return self.page


def test_get_destination_returns_the_item_under_destination() -> None:
    read = ReadResource(lambda ref: DESTINATION, DestinationRef.of, CATALOG.spec(Operation.GET_DESTINATION))

    response = read.read(request(GetDestinationRequest(destination_id="dst-7c41e9a2b5d0")))

    assert response.model_dump()["destination"]["destination_id"] == "dst-7c41e9a2b5d0"


def test_list_destinations_passes_the_paging_members_and_the_name_filter() -> None:
    recorder = Recorder(Page(items=[SUMMARY], next_token="token-2"))
    listing = ListResources(recorder, lambda request: request.tenant, CATALOG.spec(Operation.LIST_DESTINATIONS))

    response = listing.list(request(ListDestinationsRequest(name="docs", max_results=5, next_token="token-1")))

    [sent] = recorder.requests
    assert (sent.parent, sent.max_results, sent.next_token, sent.filter_value) == (TENANT, 5, "token-1", "docs")
    assert response.model_dump()["destinations"][0]["name"] == "docs"
    assert response.model_dump()["next_token"] == "token-2"


def test_list_jobs_passes_the_status_as_the_filter_value() -> None:
    job = JobSummary(
        job_id="ccj-0fdef5d64630",
        arn="arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0/custom-connector/cc-e020d65d76c0/job/ccj-0fdef5d64630",
        status=JobStatus.RUNNING,
        created_at=1.0,
        updated_at=1.0,
    )
    recorder = Recorder(Page(items=[job]))
    listing = ListResources(recorder, ConnectorRef.of, CATALOG.spec(Operation.LIST_CUSTOM_CONNECTOR_JOBS))

    listing.list(
        request(
            ListCustomConnectorJobsRequest(
                destination_id="dst-7c41e9a2b5d0", connector_id="cc-e020d65d76c0", status=JobStatus.RUNNING
            )
        )
    )

    assert recorder.requests[0].filter_value == "RUNNING"


def test_a_list_whose_parent_is_missing_answers_404_without_listing() -> None:
    recorder = Recorder(Page(items=[]))
    listing = ListResources(
        recorder,
        ConnectorRef.of,
        CATALOG.spec(Operation.LIST_CUSTOM_CONNECTOR_JOBS),
        parent_exists=lambda parent: False,
    )

    with pytest.raises(ResourceNotFoundError):
        listing.list(
            request(ListCustomConnectorJobsRequest(destination_id="dst-7c41e9a2b5d0", connector_id="cc-e020d65d76c0"))
        )

    assert recorder.requests == []


def test_a_request_carrying_the_connector_skips_the_parent_check() -> None:
    checked: list[Any] = []

    def parent_exists(parent: Any) -> bool:
        checked.append(parent)
        return False

    listing = ListResources(
        Recorder(Page(items=[])),
        ConnectorRef.of,
        CATALOG.spec(Operation.LIST_CUSTOM_CONNECTOR_JOBS),
        parent_exists=parent_exists,
    )

    listing.list(
        request(
            ListCustomConnectorJobsRequest(destination_id="dst-7c41e9a2b5d0", connector_id="cc-e020d65d76c0"),
            connector=object(),
        )
    )

    assert checked == []
