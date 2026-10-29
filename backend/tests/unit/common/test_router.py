"""Router tests for generated parsing and sanitized modeled errors."""

import json
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import MagicMock

from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from ccf_api_models.models import (
    GetCustomConnectorJobRequest,
    GetCustomConnectorRequest,
    ListCustomConnectorsRequest,
    ListCustomConnectorsResponseContent,
)

from common.delegation import TenantResolver
from common.router import RouteInvocation, RouterAdapter
from common.storage.errors import StorageNotFoundError
from common.tenant import TenantContext


class LambdaContext:
    function_name = "test"
    memory_limit_in_mb = 128
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:test"
    aws_request_id = "lambda-request"


def event(
    body: str | None = None,
    *,
    account_id: str | None = "123456789012",
    user_arn: str | None = None,
    path_parameters: dict[str, str] | None = None,
    query_parameters: dict[str, str] | None = None,
    multi_value_query_parameters: dict[str, list[str]] | None = None,
    headers: dict[str, str] | None = None,
    multi_value_headers: dict[str, list[str]] | None = None,
    request_path: str = "/connectors",
    resource_path: str = "/connectors",
) -> dict:
    identity: dict[str, str] = {} if account_id is None else {"accountId": account_id}
    if user_arn is not None:
        identity["userArn"] = user_arn
    path_parameters = path_parameters or {}
    query_parameters = query_parameters or {}
    return {
        "httpMethod": "GET",
        "path": request_path,
        "resource": resource_path,
        "headers": headers or {},
        "multiValueHeaders": multi_value_headers,
        "pathParameters": path_parameters,
        "queryStringParameters": query_parameters,
        "multiValueQueryStringParameters": multi_value_query_parameters,
        "body": body,
        "isBase64Encoded": False,
        "requestContext": {
            "requestId": "api-request-1",
            "identity": identity,
            "domainName": "trusted.execute-api.us-east-1.amazonaws.com",
            "stage": "prod",
        },
    }


_FRAMEWORK_ACCOUNT_ID = "210987654321"
_RUNTIME_ROLE_NAME = "CcfConnectorRuntime-0123456789abcdef0123456789abcdef"
_RUNTIME_ROLE_ARN = f"arn:aws:iam::{_FRAMEWORK_ACCOUNT_ID}:role/ccf-runtime/{_RUNTIME_ROLE_NAME}"


def adapter_for(app: APIGatewayRestResolver, runtime_role_arn: str | None = None) -> RouterAdapter:
    """Build an adapter whose resolver sees one optional stored connector."""
    connectors_dao = MagicMock()
    if runtime_role_arn is None:
        connectors_dao.get_connector.side_effect = StorageNotFoundError("Connector does not exist")
    else:
        connectors_dao.get_connector.return_value = SimpleNamespace(runtime_role_arn=runtime_role_arn)
    return RouterAdapter(app, TenantResolver(connectors_dao, _FRAMEWORK_ACCOUNT_ID))


def test_execute_sanitizes_internal_failure_and_keeps_request_id() -> None:
    app = APIGatewayRestResolver()
    adapter = adapter_for(app)
    secret = "customer-private-value"

    @app.get("/connectors")
    def route():
        def fail(_request):
            raise RuntimeError(secret)

        return adapter.execute(
            RouteInvocation(
                "ListCustomConnectors",
                ListCustomConnectorsRequest,
                fail,
                HTTPStatus.OK,
            )
        )

    response = app.resolve(event(), LambdaContext())
    body = json.loads(response["body"])

    assert response["statusCode"] == 500
    assert body == {
        "code": "INTERNAL_SERVER_ERROR",
        "message": "The service could not complete the request",
        "request_id": "api-request-1",
    }
    assert secret not in response["body"]


def test_execute_missing_tenant_is_safe() -> None:
    app = APIGatewayRestResolver()
    adapter = adapter_for(app)

    @app.get("/connectors")
    def route():
        return adapter.execute(
            RouteInvocation(
                "ListCustomConnectors",
                ListCustomConnectorsRequest,
                MagicMock(return_value=ListCustomConnectorsResponseContent(connectors=[])),
                HTTPStatus.OK,
            )
        )

    response = app.resolve(event(account_id=None), LambdaContext())
    body = json.loads(response["body"])

    assert response["statusCode"] == 400
    assert body["request_id"] == "api-request-1"
    assert body["code"] == "BAD_REQUEST"


def test_execute_malformed_and_non_object_json_are_bad_requests() -> None:
    for raw_body in ("{", "[]"):
        app = APIGatewayRestResolver()
        adapter = adapter_for(app)

        @app.get("/connectors")
        def route(adapter: RouterAdapter = adapter):
            return adapter.execute(
                RouteInvocation(
                    "ListCustomConnectors",
                    ListCustomConnectorsRequest,
                    MagicMock(),
                    HTTPStatus.OK,
                )
            )

        response = app.resolve(event(raw_body), LambdaContext())
        assert response["statusCode"] == 400


def test_execute_rejects_undeclared_and_duplicate_query_parameters() -> None:
    for query_parameters, multi_value_query_parameters in (
        ({"undeclared": "value"}, None),
        ({"max_results": "10"}, {"max_results": ["10", "20"]}),
    ):
        app = APIGatewayRestResolver()
        adapter = adapter_for(app)
        activity = MagicMock()

        @app.get("/connectors")
        def route(adapter: RouterAdapter = adapter, activity: MagicMock = activity):
            return adapter.execute(
                RouteInvocation(
                    "ListCustomConnectors",
                    ListCustomConnectorsRequest,
                    activity,
                    HTTPStatus.OK,
                    frozenset({"max_results", "next_token"}),
                )
            )

        response = app.resolve(
            event(
                query_parameters=query_parameters,
                multi_value_query_parameters=multi_value_query_parameters,
            ),
            LambdaContext(),
        )

        assert response["statusCode"] == 400
        activity.assert_not_called()


def test_execute_rejects_connector_identity_outside_path() -> None:
    for body, query_parameters in (
        ('{"connector_id":"query-connector"}', {}),
        (None, {"connector_id": "query-connector"}),
    ):
        app = APIGatewayRestResolver()
        adapter = adapter_for(app)
        activity = MagicMock()

        @app.get("/connectors/<connector_id>")
        def route(connector_id: str, adapter: RouterAdapter = adapter, activity: MagicMock = activity):
            del connector_id
            return adapter.execute(
                RouteInvocation(
                    "GetCustomConnector",
                    GetCustomConnectorRequest,
                    activity,
                    HTTPStatus.OK,
                )
            )

        response = app.resolve(
            event(
                body,
                path_parameters={"connector_id": "path-connector"},
                query_parameters=query_parameters,
                request_path="/connectors/path-connector",
                resource_path="/connectors/{connector_id}",
            ),
            LambdaContext(),
        )

        assert response["statusCode"] == 400
        activity.assert_not_called()


def test_execute_uses_path_connector_identity() -> None:
    app = APIGatewayRestResolver()
    adapter = adapter_for(app)
    activity = MagicMock(return_value=None)

    @app.get("/connectors/<connector_id>")
    def route(connector_id: str):
        del connector_id
        return adapter.execute(
            RouteInvocation(
                "GetCustomConnector",
                GetCustomConnectorRequest,
                activity,
                HTTPStatus.NO_CONTENT,
            )
        )

    response = app.resolve(
        event(
            path_parameters={"connector_id": "path-connector"},
            request_path="/connectors/path-connector",
            resource_path="/connectors/{connector_id}",
        ),
        LambdaContext(),
    )

    assert response["statusCode"] == 204
    request = activity.call_args.args[0]
    assert request.payload.connector_id == "path-connector"
    assert request.service_endpoint == ("https://trusted.execute-api.us-east-1.amazonaws.com/prod")
    assert not hasattr(request, "api_endpoint")


def test_execute_rejects_body_and_query_duplicate() -> None:
    app = APIGatewayRestResolver()
    adapter = adapter_for(app)
    activity = MagicMock()

    @app.get("/connectors")
    def route():
        return adapter.execute(
            RouteInvocation(
                "ListCustomConnectors",
                ListCustomConnectorsRequest,
                activity,
                HTTPStatus.OK,
                frozenset({"max_results", "next_token"}),
            )
        )

    response = app.resolve(
        event('{"max_results":10}', query_parameters={"max_results": "20"}),
        LambdaContext(),
    )

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_execute_rejects_job_identity_outside_path() -> None:
    app = APIGatewayRestResolver()
    adapter = adapter_for(app)
    activity = MagicMock()

    @app.get("/connectors/<connector_id>/jobs/<job_id>")
    def route(connector_id: str, job_id: str):
        del connector_id, job_id
        return adapter.execute(
            RouteInvocation(
                "GetCustomConnectorJob",
                GetCustomConnectorJobRequest,
                activity,
                HTTPStatus.OK,
            )
        )

    response = app.resolve(
        event(
            path_parameters={"connector_id": "path-connector", "job_id": "path-job"},
            query_parameters={"job_id": "query-job"},
            request_path="/connectors/path-connector/jobs/path-job",
            resource_path="/connectors/{connector_id}/jobs/{job_id}",
        ),
        LambdaContext(),
    )

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_execute_derives_service_endpoint_only_from_api_gateway_context() -> None:
    app = APIGatewayRestResolver()
    adapter = adapter_for(app)
    activity = MagicMock(return_value=None)

    @app.get("/connectors/<connector_id>")
    def route(connector_id: str):
        del connector_id
        return adapter.execute(
            RouteInvocation(
                "GetCustomConnector",
                GetCustomConnectorRequest,
                activity,
                HTTPStatus.NO_CONTENT,
            )
        )

    request_event = event(
        path_parameters={"connector_id": "path-controlled-connector"},
        request_path="/connectors/path-controlled-connector",
        resource_path="/connectors/{connector_id}",
    )
    request_event["headers"] = {
        "Host": "header-controlled.example.com",
        "X-Forwarded-Host": "forwarded-controlled.example.com",
    }
    request_event["requestContext"]["stage"] = "stage/name"

    response = app.resolve(request_event, LambdaContext())

    assert response["statusCode"] == 204
    request = activity.call_args.args[0]
    assert request.service_endpoint == ("https://trusted.execute-api.us-east-1.amazonaws.com/stage%2Fname")
    assert "controlled" not in request.service_endpoint


def _delegated_route(
    runtime_role_arn: str | None,
    request_event: dict,
    activity: MagicMock,
) -> dict:
    app = APIGatewayRestResolver()
    adapter = adapter_for(app, runtime_role_arn)

    @app.get("/connectors/<connector_id>")
    def route(connector_id: str):
        del connector_id
        return adapter.execute(
            RouteInvocation(
                "GetCustomConnector",
                GetCustomConnectorRequest,
                activity,
                HTTPStatus.NO_CONTENT,
            )
        )

    return app.resolve(request_event, LambdaContext())


def _delegated_event(
    *,
    account_id: str | None = _FRAMEWORK_ACCOUNT_ID,
    user_arn: str | None = f"arn:aws:sts::{_FRAMEWORK_ACCOUNT_ID}:assumed-role/{_RUNTIME_ROLE_NAME}/ccf-connector",
    tenant_arn_prefix: str = "arn:aws:ccf:us-east-1:123456789012",
    multi_value_headers: dict[str, list[str]] | None = None,
) -> dict:
    return event(
        account_id=account_id,
        user_arn=user_arn,
        headers={"x-ccf-tenant-arn-prefix": tenant_arn_prefix},
        multi_value_headers=multi_value_headers,
        path_parameters={"connector_id": "cc-1"},
        request_path="/connectors/cc-1",
        resource_path="/connectors/{connector_id}",
    )


def test_delegated_runtime_role_acts_for_the_claimed_tenant() -> None:
    activity = MagicMock(return_value=None)

    response = _delegated_route(_RUNTIME_ROLE_ARN, _delegated_event(), activity)

    assert response["statusCode"] == 204
    request = activity.call_args.args[0]
    assert request.tenant == TenantContext("123456789012", "us-east-1", "aws")
    assert request.payload.connector_id == "cc-1"


def test_delegated_header_from_a_client_caller_is_rejected() -> None:
    activity = MagicMock()

    response = _delegated_route(
        _RUNTIME_ROLE_ARN,
        _delegated_event(
            account_id="555555555555",
            user_arn="arn:aws:sts::555555555555:assumed-role/ClientRole/session",
        ),
        activity,
    )

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_delegated_header_from_another_connectors_runtime_role_is_rejected() -> None:
    activity = MagicMock()
    other_runtime_role_arn = (
        f"arn:aws:iam::{_FRAMEWORK_ACCOUNT_ID}:role/ccf-runtime/CcfConnectorRuntime-ffffffffffffffffffffffffffffffff"
    )

    response = _delegated_route(other_runtime_role_arn, _delegated_event(), activity)

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_delegated_header_is_rejected_when_the_connector_has_no_runtime_role() -> None:
    activity = MagicMock()
    app = APIGatewayRestResolver()
    connectors_dao = MagicMock()
    connectors_dao.get_connector.return_value = SimpleNamespace(runtime_role_arn=None)
    adapter = RouterAdapter(app, TenantResolver(connectors_dao, _FRAMEWORK_ACCOUNT_ID))

    @app.get("/connectors/<connector_id>")
    def route(connector_id: str):
        del connector_id
        return adapter.execute(
            RouteInvocation(
                "GetCustomConnector",
                GetCustomConnectorRequest,
                activity,
                HTTPStatus.NO_CONTENT,
            )
        )

    response = app.resolve(_delegated_event(), LambdaContext())

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_delegated_header_for_an_unknown_claimed_tenant_is_not_found() -> None:
    activity = MagicMock()

    response = _delegated_route(None, _delegated_event(), activity)

    assert response["statusCode"] == 404
    activity.assert_not_called()


def test_delegated_header_without_an_assumed_role_caller_is_rejected() -> None:
    activity = MagicMock()

    response = _delegated_route(
        _RUNTIME_ROLE_ARN,
        _delegated_event(user_arn=f"arn:aws:iam::{_FRAMEWORK_ACCOUNT_ID}:user/not-a-role"),
        activity,
    )

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_delegated_header_supplied_more_than_once_is_rejected() -> None:
    activity = MagicMock()

    response = _delegated_route(
        _RUNTIME_ROLE_ARN,
        _delegated_event(
            multi_value_headers={
                "x-ccf-tenant-arn-prefix": [
                    "arn:aws:ccf:us-east-1:123456789012",
                    "arn:aws:ccf:us-east-1:999999999999",
                ]
            }
        ),
        activity,
    )

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_delegated_header_representation_mismatch_is_rejected() -> None:
    activity = MagicMock()

    response = _delegated_route(
        _RUNTIME_ROLE_ARN,
        _delegated_event(multi_value_headers={"x-ccf-tenant-arn-prefix": ["arn:aws:ccf:us-east-1:999999999999"]}),
        activity,
    )

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_malformed_delegated_tenant_prefix_is_rejected() -> None:
    activity = MagicMock()

    response = _delegated_route(
        _RUNTIME_ROLE_ARN,
        _delegated_event(tenant_arn_prefix="arn:aws:ccf:us-east-1:123456789012:custom-connector/cc-1"),
        activity,
    )

    assert response["statusCode"] == 400
    activity.assert_not_called()


def test_delegated_header_on_a_tenant_wide_operation_is_rejected() -> None:
    activity = MagicMock()
    app = APIGatewayRestResolver()
    adapter = adapter_for(app, _RUNTIME_ROLE_ARN)

    @app.get("/connectors")
    def route():
        return adapter.execute(
            RouteInvocation(
                "ListCustomConnectors",
                ListCustomConnectorsRequest,
                activity,
                HTTPStatus.OK,
                frozenset({"max_results", "next_token"}),
            )
        )

    response = app.resolve(
        event(
            account_id=_FRAMEWORK_ACCOUNT_ID,
            user_arn=f"arn:aws:sts::{_FRAMEWORK_ACCOUNT_ID}:assumed-role/{_RUNTIME_ROLE_NAME}/ccf-connector",
            headers={"x-ccf-tenant-arn-prefix": "arn:aws:ccf:us-east-1:123456789012"},
        ),
        LambdaContext(),
    )

    assert response["statusCode"] == 400
    activity.assert_not_called()
