"""Runs one API operation: binds the request, resolves the caller, runs the activity, and maps every error.

A 4xx response is logged at WARNING with the error type (and, for a bad request, only the
names of the members at fault); a 5xx response is logged at ERROR with the error type and its
stack frames, never a message or a value. Each mapped error publishes one `ApiErrors` count.
Every response carries the request's correlation ID: the caller's, or a new UUID.
"""

from __future__ import annotations

import json
import traceback
import uuid
from http import HTTPStatus
from typing import Any, Protocol

from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
    IdempotencyValidationError,
)
from ccf_api_models.base import CcfModel, ValueModel
from ccf_api_models.catalog import ERROR_STATUSES, HeaderMember, OperationSpec
from ccf_api_models.models import ErrorResponse
from pydantic import ConfigDict

from common.authenticated_call import AuthenticatedCall
from common.errors import InternalServiceError, ResourceConflictError, ServiceError
from common.idempotency import IdempotentCreates, IdempotentRunRequest
from common.observability import MetricDimensionName, MetricName, MetricRecord, logger
from common.operation import Activity, OperationRequest, RequestTrace, ResolveTenantRequest, TenantResolution
from common.request_binder import RequestBinder

APPLICATION_JSON = "application/json"


class MetricsRecorder(Protocol):
    def record(self, metric: MetricRecord) -> None: ...


class Route(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: OperationSpec
    activity: Activity


class _Reply(ValueModel):
    """What one response answers with: its status, its body, and the correlation ID it returns."""

    spec: OperationSpec
    trace: RequestTrace
    status: HTTPStatus
    body: str | None


class _Failure(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: OperationSpec
    trace: RequestTrace
    error: ServiceError
    cause: BaseException
    """The exception raised; only its type and stack frames are logged."""


class RouterAdapter:
    def __init__(
        self,
        app: APIGatewayRestResolver,
        binder: RequestBinder,
        tenants: TenantResolution,
        idempotency: IdempotentCreates,
        metrics: MetricsRecorder,
    ) -> None:
        self._app = app
        self._binder = binder
        self._tenants = tenants
        self._idempotency = idempotency
        self._metrics = metrics

    def execute(self, route: Route) -> Response[str]:
        spec = route.spec
        trace = RequestTrace(correlation_id=str(uuid.uuid4()), api_request_id=self._request_id())
        logger.append_keys(correlation_id=trace.correlation_id)
        try:
            payload = self._binder.bind(spec)
            sent: str | None = getattr(payload, HeaderMember.CORRELATION_ID)
            if sent is not None:
                trace = RequestTrace(correlation_id=sent, api_request_id=trace.api_request_id)
                logger.append_keys(correlation_id=sent)
            caller = self._tenants.resolve(
                ResolveTenantRequest(
                    call=AuthenticatedCall.from_event(self._app.current_event.raw_event),
                    payload=payload,
                    callers=spec.callers,
                )
            )
            request: OperationRequest[CcfModel] = OperationRequest(
                tenant=caller.tenant, payload=payload, trace=trace, connector=caller.connector
            )
            response = self._idempotency.run(IdempotentRunRequest(spec=spec, request=request, activity=route.activity))
        except ServiceError as error:
            return self._failed(_Failure(spec=spec, trace=trace, error=error, cause=error))
        except (IdempotencyValidationError, IdempotencyAlreadyInProgressError) as error:
            conflict = ResourceConflictError("The client token names another call")
            return self._failed(_Failure(spec=spec, trace=trace, error=conflict, cause=error))
        except Exception as error:
            internal = InternalServiceError("The activity raised an unexpected error")
            return self._failed(_Failure(spec=spec, trace=trace, error=internal, cause=error))
        logger.info("api_operation_succeeded", extra={"operation": spec.operation.value})
        body = None if response is None else json.dumps(response.model_dump_for_wire())
        return self._reply(_Reply(spec=spec, trace=trace, status=spec.success_status, body=body))

    def _request_id(self) -> str:
        """API Gateway's request ID, or the Lambda invocation's when an event carries none."""
        request_id = self._app.current_event.request_context.request_id
        if request_id:
            return request_id
        invocation_id: str = self._app.lambda_context.aws_request_id
        logger.warning("api_request_id_missing", extra={"lambda_request_id": invocation_id})
        return invocation_id

    def _failed(self, failure: _Failure) -> Response[str]:
        error = failure.error
        status = ERROR_STATUSES[error.code]
        extra: dict[str, Any] = {
            "operation": failure.spec.operation.value,
            "status": status.value,
            "error_type": type(failure.cause).__name__,
        }
        if error.aws_error_code is not None:
            extra["aws_error_code"] = error.aws_error_code
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            frames = traceback.extract_tb(failure.cause.__traceback__)
            extra["frames"] = [f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in frames]
            logger.error("api_operation_failed", extra=extra)
        else:
            if error.fields:
                extra["fields"] = list(error.fields)
            logger.warning("api_operation_rejected", extra=extra)
        self._metrics.record(
            MetricRecord(
                name=MetricName.API_ERRORS,
                value=1,
                dimensions={
                    MetricDimensionName.OPERATION: failure.spec.operation.value,
                    MetricDimensionName.ERROR_CODE: error.code.value,
                },
            )
        )
        modeled = ErrorResponse(code=error.code, message=error.public_message, request_id=failure.trace.api_request_id)
        body = json.dumps(modeled.model_dump_for_wire())
        return self._reply(_Reply(spec=failure.spec, trace=failure.trace, status=status, body=body))

    @staticmethod
    def _reply(reply: _Reply) -> Response[str]:
        binding = reply.spec.binding_of(HeaderMember.CORRELATION_ID)
        headers = {} if binding is None else {binding.wire_name: reply.trace.correlation_id}
        return Response(status_code=reply.status, content_type=APPLICATION_JSON, body=reply.body, headers=headers)
