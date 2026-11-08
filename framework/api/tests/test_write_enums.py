"""The enum writer turns a two-operation OpenAPI document and a metrics contract into Python source."""

from typing import Any

from ccf_api_models.contracts import JobContainerContract, MetricsContract
from write_enums import JobContainerWriter, MetricNamesWriter, OperationsWriter


def _openapi() -> dict[str, Any]:
    body = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/GetThingResponseContent"}}}}
    return {
        "paths": {
            "/things/{id}": {
                "get": {
                    "operationId": "GetThing",
                    "x-ccf-request-model": "GetThingRequest",
                    "responses": {"200": body},
                },
                "delete": {
                    "operationId": "DeleteThing",
                    "x-ccf-request-model": "DeleteThingRequest",
                    "responses": {"204": {"description": "No body"}, "400": body},
                },
            }
        }
    }


def test_operations_are_sorted_by_operation_id() -> None:
    source = OperationsWriter().write(_openapi())

    assert '    DELETE_THING = "DeleteThing"\n    GET_THING = "GetThing"\n' in source


def test_each_operation_names_its_request_and_response_models() -> None:
    source = OperationsWriter().write(_openapi())

    assert "Operation.GET_THING: OperationModels(request=GetThingRequest, response=GetThingResponseContent)" in source
    assert "Operation.DELETE_THING: OperationModels(request=DeleteThingRequest, response=None)" in source


def test_an_operation_without_a_response_body_returns_none_from_call() -> None:
    source = OperationsWriter().write(_openapi())

    assert "    def call(self, request: DeleteThingRequest) -> None: ..." in source
    assert "    def call(self, request: GetThingRequest) -> GetThingResponseContent: ..." in source


def test_metric_names_come_from_the_contract() -> None:
    contract = MetricsContract.model_validate(
        {
            "namespace": "Things",
            "dimensions": {"STATUS": "Status"},
            "metrics": {"THINGS_MADE": {"name": "ThingsMade", "unit": "Count", "dimensionSets": [["STATUS"]]}},
        }
    )

    source = MetricNamesWriter().write(contract)

    assert all(
        line in source
        for line in ('METRICS_NAMESPACE: Final = "Things"', 'THINGS_MADE = "ThingsMade"', 'STATUS = "Status"')
    )


def test_the_job_container_enums_come_from_the_contract() -> None:
    contract = JobContainerContract.model_validate(
        {
            "containers": {"connector": "app", "logRouter": "router"},
            "variables": {"jobId": "THING_JOB_ID"},
            "exitCodes": {"SUCCEEDED": 0, "THING_FAILED": 9},
        }
    )

    source = JobContainerWriter().write(contract)

    assert all(
        line in source
        for line in (
            'CONNECTOR_CONTAINER: Final = "app"',
            'LOG_ROUTER_CONTAINER: Final = "router"',
            '    JOB_ID = "THING_JOB_ID"',
            "    THING_FAILED = 9",
        )
    )
