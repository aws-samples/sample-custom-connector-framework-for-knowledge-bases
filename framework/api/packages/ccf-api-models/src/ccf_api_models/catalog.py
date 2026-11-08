"""How each operation travels over HTTP and who may call it, read once from the packaged `openapi.json`."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from http import HTTPStatus
from importlib.resources import files
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ccf_api_models.base import CcfModel, ValueModel
from ccf_api_models.models import ErrorCode
from ccf_api_models.operations import OPERATION_MODELS, Operation

ERROR_STATUSES: Final[Mapping[ErrorCode, HTTPStatus]] = {
    ErrorCode.BAD_REQUEST: HTTPStatus.BAD_REQUEST,
    ErrorCode.UNAUTHORIZED: HTTPStatus.FORBIDDEN,
    ErrorCode.RESOURCE_NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.THROTTLED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.SERVICE_UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.INTERNAL_SERVER_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
}
"""The status each error code is returned with."""


class Caller(StrEnum):
    """Who the `ccf#callers` trait admits: a tenant principal, or a custom connector's runtime role."""

    TENANT = "TENANT"
    RUNTIME = "RUNTIME"


class HeaderMember(StrEnum):
    """The members of the `CallerHeaders` mixin every operation input has."""

    CORRELATION_ID = "correlation_id"
    TENANT_ARN_PREFIX = "tenant_arn_prefix"


class ParameterLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"


class SchemaType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class HttpMethod(StrEnum):
    GET = "GET"
    PUT = "PUT"
    POST = "POST"
    DELETE = "DELETE"
    PATCH = "PATCH"


class ParameterBinding(ValueModel):
    """One request member that travels in the path, the query string, or a header."""

    member_name: str
    wire_name: str
    location: ParameterLocation
    schema_type: SchemaType


class Pagination(ValueModel):
    """The Smithy `@paginated` trait, read from `x-ccf-paginated`."""

    model_config = ConfigDict(alias_generator=to_camel)

    input_token: str
    output_token: str
    page_size: str
    items: str


class OperationSpec(ValueModel):
    operation: Operation
    method: HttpMethod
    path: str
    """The OpenAPI path template, such as `/api/v1/destinations/{destination_id}`."""
    success_status: HTTPStatus
    request_model: type[CcfModel]
    response_model: type[CcfModel] | None
    parameters: tuple[ParameterBinding, ...]
    body_members: frozenset[str]
    callers: frozenset[Caller]
    client_token_member: str | None
    retry_safe: bool
    pagination: Pagination | None

    def binding_of(self, member_name: str) -> ParameterBinding | None:
        return next((binding for binding in self.parameters if binding.member_name == member_name), None)

    @property
    def filter_member(self) -> str | None:
        """The one query member besides the pagination members, such as `name` on `ListDestinations`."""
        paging = set() if self.pagination is None else {self.pagination.input_token, self.pagination.page_size}
        filters = [
            binding.member_name
            for binding in self.parameters
            if binding.location is ParameterLocation.QUERY and binding.member_name not in paging
        ]
        if len(filters) > 1:
            raise ValueError(f"{self.operation} has more than one filter member: {filters}")
        return filters[0] if filters else None


class _OpenApiValue(BaseModel):
    """A part of `openapi.json`; the parts the catalog doesn't read are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _ParameterSchema(_OpenApiValue):
    type: SchemaType = SchemaType.STRING
    """Absent on an enum parameter, whose schema references the string enum."""


class _Parameter(_OpenApiValue):
    name: str
    location: ParameterLocation = Field(alias="in")
    member_name: str = Field(alias="x-ccf-member-name")
    parameter_schema: _ParameterSchema = Field(alias="schema")


class _OpenApiOperation(_OpenApiValue):
    operation_id: Operation = Field(alias="operationId")
    parameters: tuple[_Parameter, ...] = ()
    responses: Mapping[str, object]
    request_model: str = Field(alias="x-ccf-request-model")
    callers: frozenset[Caller] = Field(alias="x-ccf-callers")
    client_token_member: str | None = Field(default=None, alias="x-ccf-client-token-member")
    retry_safe: bool = Field(alias="x-ccf-retry-safe")
    pagination: Pagination | None = Field(default=None, alias="x-ccf-paginated")


class _Schema(_OpenApiValue):
    properties: Mapping[str, object] = {}


class _Components(_OpenApiValue):
    schemas: Mapping[str, _Schema]


class OpenApiDocument(_OpenApiValue):
    paths: Mapping[str, Mapping[str, _OpenApiOperation]]
    components: _Components


class _PathOperation(ValueModel):
    path: str
    method: HttpMethod
    operation: _OpenApiOperation
    request_members: frozenset[str]
    """The members of the operation's complete input schema."""


class OperationCatalog:
    """Every operation's `OperationSpec`."""

    def __init__(self, specs: Mapping[Operation, OperationSpec]) -> None:
        self._specs = specs

    @classmethod
    def load(cls) -> OperationCatalog:
        """Read the packaged `openapi.json`; an operation without both `CallerHeaders` members fails."""
        document = OpenApiDocument.model_validate_json(files("ccf_api_models").joinpath("openapi.json").read_bytes())
        specs = [
            cls._spec_of(
                _PathOperation(
                    path=path,
                    method=HttpMethod(method.upper()),
                    operation=operation,
                    request_members=frozenset(document.components.schemas[operation.request_model].properties),
                )
            )
            for path, path_item in document.paths.items()
            for method, operation in path_item.items()
        ]
        return cls({spec.operation: spec for spec in specs})

    def spec(self, operation: Operation) -> OperationSpec:
        return self._specs[operation]

    def spec_for(self, request_type: type[CcfModel]) -> OperationSpec:
        """Return the spec of the operation whose input is `request_type`."""
        return next(spec for spec in self._specs.values() if spec.request_model is request_type)

    def specs(self) -> tuple[OperationSpec, ...]:
        return tuple(self._specs.values())

    @staticmethod
    def _spec_of(entry: _PathOperation) -> OperationSpec:
        operation = entry.operation
        parameters = tuple(
            ParameterBinding(
                member_name=parameter.member_name,
                wire_name=parameter.name,
                location=parameter.location,
                schema_type=parameter.parameter_schema.type,
            )
            for parameter in operation.parameters
        )
        headers = {binding.member_name for binding in parameters if binding.location is ParameterLocation.HEADER}
        missing_headers = sorted(str(member) for member in set(HeaderMember) - headers)
        if missing_headers:
            raise ValueError(f"{operation.operation_id} lacks a CallerHeaders member: {missing_headers}")
        [success_status] = [HTTPStatus(int(status)) for status in operation.responses if status.startswith("2")]
        models = OPERATION_MODELS[operation.operation_id]
        return OperationSpec(
            operation=operation.operation_id,
            method=entry.method,
            path=entry.path,
            success_status=success_status,
            request_model=models.request,
            response_model=models.response,
            parameters=parameters,
            body_members=entry.request_members - {binding.member_name for binding in parameters},
            callers=operation.callers,
            client_token_member=operation.client_token_member,
            retry_safe=operation.retry_safe,
            pagination=operation.pagination,
        )
