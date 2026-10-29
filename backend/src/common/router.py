"""Generated-model parsing and sanitized API response mapping."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from ccf_api_models.base import CcfModel
from ccf_api_models.models import ErrorCode, ErrorResponse
from pydantic import ValidationError

from common.configuration import ConfigurationDecryptionError, ConfigurationValidationError
from common.delegation import ResolveTenantRequest, TenantResolver
from common.observability import logger
from common.operation import OperationRequest
from common.runtime_role import RuntimeRoleError
from common.storage.errors import StorageConflictError, StorageNotFoundError, StorageOperationError
from common.tenant import TenantContextError

APPLICATION_JSON = "application/json"
_API_GATEWAY_DOMAIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_REST_API_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,128}$")


class RequestParsingError(ValueError):
    """Raised when an HTTP request cannot form a generated input model."""


@dataclass(frozen=True)
class RouteInvocation:
    """One generated request model, activity, success status, and query contract."""

    operation: str
    request_model: type[CcfModel]
    activity: Callable[[OperationRequest[Any]], CcfModel | None]
    success_status: HTTPStatus
    query_members: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ErrorMapping:
    """Fixed client-safe error contract."""

    status: HTTPStatus
    code: ErrorCode
    message: str


_BAD_REQUEST = ErrorMapping(HTTPStatus.BAD_REQUEST, ErrorCode.BAD_REQUEST, "The request is invalid")
_NOT_FOUND = ErrorMapping(HTTPStatus.NOT_FOUND, ErrorCode.RESOURCE_NOT_FOUND, "The requested resource was not found")
_CONFLICT = ErrorMapping(HTTPStatus.CONFLICT, ErrorCode.CONFLICT, "The request conflicts with the resource state")
_INTERNAL = ErrorMapping(
    HTTPStatus.INTERNAL_SERVER_ERROR,
    ErrorCode.INTERNAL_SERVER_ERROR,
    "The service could not complete the request",
)


class RouterAdapter:
    """Executes generated parsing and centralized response/error mapping."""

    def __init__(self, app: APIGatewayRestResolver, tenant_resolver: TenantResolver) -> None:
        self._app = app
        self._tenant_resolver = tenant_resolver

    def execute(self, invocation: RouteInvocation) -> Response[str]:
        """Parse, authorize by tenant identity, invoke, and serialize once."""
        request_id = self._request_id()
        try:
            tenant = self._tenant_resolver.resolve(
                ResolveTenantRequest(
                    event=self._app.current_event.raw_event,
                    path_parameters=dict(self._app.current_event.path_parameters or {}),
                )
            )
            payload = self._parse(invocation)
            response = invocation.activity(
                OperationRequest(
                    tenant=tenant,
                    payload=payload,
                    service_endpoint=self._service_endpoint(),
                    rest_api_id=self._rest_api_id(),
                )
            )
        except (
            json.JSONDecodeError,
            ValidationError,
            TenantContextError,
            ConfigurationValidationError,
            RequestParsingError,
        ) as error:
            return self._error_response(invocation.operation, request_id, _BAD_REQUEST, error)
        except StorageNotFoundError as error:
            return self._error_response(invocation.operation, request_id, _NOT_FOUND, error)
        except StorageConflictError as error:
            return self._error_response(invocation.operation, request_id, _CONFLICT, error)
        except (StorageOperationError, ConfigurationDecryptionError, RuntimeRoleError) as error:
            return self._error_response(invocation.operation, request_id, _INTERNAL, error)
        except Exception as error:
            return self._error_response(invocation.operation, request_id, _INTERNAL, error)
        logger.info(
            "api_operation_succeeded",
            extra={"operation": invocation.operation, "request_id": request_id},
        )
        body = None if response is None else json.dumps(response.model_dump_for_wire())
        return Response(
            status_code=invocation.success_status,
            content_type=APPLICATION_JSON,
            body=body,
        )

    def _parse(self, invocation: RouteInvocation) -> CcfModel:
        raw_body = self._app.current_event.body
        body: Any = {} if raw_body in (None, "") else json.loads(raw_body)
        if not isinstance(body, dict):
            raise RequestParsingError("Request body must be an object")
        path = dict(self._app.current_event.path_parameters or {})
        query = self._query_parameters()
        self._reject_duplicate_sources(body, path, query)
        unknown_query_members = query.keys() - invocation.query_members
        if unknown_query_members:
            raise RequestParsingError("Query parameter is not modeled for this operation")
        parsed_query = self._parse_query(query)
        combined = {**body, **parsed_query, **path}
        return invocation.request_model.model_validate_json(json.dumps(combined))

    def _query_parameters(self) -> dict[str, str]:
        query = dict(self._app.current_event.query_string_parameters or {})
        raw_multi_value_query = self._app.current_event.raw_event.get("multiValueQueryStringParameters")
        if raw_multi_value_query is None:
            return query
        if not isinstance(raw_multi_value_query, dict):
            raise RequestParsingError("Multi-value query parameters must be an object")
        single_value_query: dict[str, str] = {}
        for name, values in raw_multi_value_query.items():
            if not isinstance(name, str) or not isinstance(values, list) or len(values) != 1:
                raise RequestParsingError("Query parameter must be provided exactly once")
            value = values[0]
            if not isinstance(value, str):
                raise RequestParsingError("Query parameter value must be a string")
            single_value_query[name] = value
        if query and query != single_value_query:
            raise RequestParsingError("Query parameter representations do not match")
        return single_value_query

    @staticmethod
    def _reject_duplicate_sources(
        body: dict[str, Any],
        path: dict[str, str],
        query: dict[str, str],
    ) -> None:
        if (body.keys() & path.keys()) or (body.keys() & query.keys()) or (path.keys() & query.keys()):
            raise RequestParsingError("Request member must come from exactly one HTTP source")

    @staticmethod
    def _parse_query(query: dict[str, str]) -> dict[str, Any]:
        parsed: dict[str, Any] = dict(query)
        if "max_results" not in parsed:
            return parsed
        try:
            parsed["max_results"] = int(parsed["max_results"])
        except ValueError as error:
            raise RequestParsingError("max_results must be an integer") from error
        return parsed

    def _service_endpoint(self) -> str | None:
        request_context = self._app.current_event.raw_event.get("requestContext", {})
        if not isinstance(request_context, dict):
            return None
        domain_name = request_context.get("domainName")
        stage = request_context.get("stage")
        if (
            not isinstance(domain_name, str)
            or not _API_GATEWAY_DOMAIN_PATTERN.fullmatch(domain_name)
            or not isinstance(stage, str)
            or not stage
        ):
            return None
        return f"https://{domain_name}/{quote(stage, safe='')}"

    def _rest_api_id(self) -> str | None:
        """Return the API Gateway-authored REST API id serving this request."""
        request_context = self._app.current_event.raw_event.get("requestContext", {})
        if not isinstance(request_context, dict):
            return None
        rest_api_id = request_context.get("apiId")
        if not isinstance(rest_api_id, str) or _REST_API_ID_PATTERN.fullmatch(rest_api_id) is None:
            return None
        return rest_api_id

    def _request_id(self) -> str:
        request_context = self._app.current_event.raw_event.get("requestContext", {})
        request_id = request_context.get("requestId") if isinstance(request_context, dict) else None
        return request_id if isinstance(request_id, str) and request_id else "request-id-unavailable"

    @staticmethod
    def _error_response(
        operation: str,
        request_id: str,
        mapping: ErrorMapping,
        error: Exception,
    ) -> Response[str]:
        logger.error(
            "api_operation_failed",
            extra={
                "operation": operation,
                "request_id": request_id,
                "error_type": type(error).__name__,
            },
        )
        modeled = ErrorResponse(
            code=mapping.code,
            message=mapping.message,
            request_id=request_id,
        )
        return Response(
            status_code=mapping.status,
            content_type=APPLICATION_JSON,
            body=json.dumps(modeled.model_dump_for_wire()),
        )
