"""API composition-root tests that do not call AWS services."""

import json

from api_handler import get_connector_activity, handler
from common.env import (
    AWS_PARTITION,
    AWS_REGION,
    CCF_SERVICE_PRINCIPAL,
    CUSTOM_CONNECTOR_API_ENDPOINT,
    FRAMEWORK_ACCOUNT_ID,
    INGESTION_BUCKET_NAME,
)


class LambdaContext:
    function_name = "test"
    memory_limit_in_mb = 128
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:test"
    aws_request_id = "lambda-request"


def test_handler_uses_modeled_error_for_missing_tenant() -> None:
    event = {
        "httpMethod": "GET",
        "path": "/api/v1/custom-connectors",
        "resource": "/api/v1/custom-connectors",
        "headers": {},
        "pathParameters": {},
        "queryStringParameters": {},
        "body": None,
        "isBase64Encoded": False,
        "requestContext": {"requestId": "api-request-1", "identity": {}},
    }

    response = handler(event, LambdaContext())
    body = json.loads(response["body"])

    assert response["statusCode"] == 400
    assert body["code"] == "BAD_REQUEST"
    assert body["request_id"] == "api-request-1"


def test_get_connector_activity_uses_deployment_owned_service_reference_values() -> None:
    deployment = get_connector_activity._service_reference_configuration

    assert deployment.account_id == FRAMEWORK_ACCOUNT_ID
    assert deployment.endpoint == (CUSTOM_CONNECTOR_API_ENDPOINT or "")
    assert deployment.region == AWS_REGION
    assert deployment.partition == AWS_PARTITION
    assert deployment.service_principal == CCF_SERVICE_PRINCIPAL
    assert deployment.ingestion_bucket_name == INGESTION_BUCKET_NAME
