"""Builds an operation's generated request model from the path, query string, headers, and JSON body.

Where each member travels comes from the operation's `OperationSpec`. Every rejection is a
`BadRequestError` that names the members at fault and holds no value from the request.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from ccf_api_models.base import CcfModel
from ccf_api_models.catalog import OperationSpec, ParameterBinding, ParameterLocation, SchemaType
from pydantic import TypeAdapter, ValidationError

from common.errors import BadRequestError

_JSON_OBJECT: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])
_BOOLEANS = {"true": True, "false": False}


class RequestBinder:
    def __init__(self, app: APIGatewayRestResolver) -> None:
        self._app = app

    def bind(self, spec: OperationSpec) -> CcfModel:
        body = self._body()
        sent = self._parameters(spec)
        repeated = sorted(body.keys() & sent.keys())
        if repeated:
            raise BadRequestError("A member came from two sources", fields=tuple(repeated))
        try:
            return spec.request_model.model_validate_json(json.dumps({**body, **sent}))
        except ValidationError as error:
            fields = tuple(".".join(str(part) for part in detail["loc"]) for detail in error.errors())
            raise BadRequestError("The request breaks the operation's input model", fields=fields) from error

    def _body(self) -> dict[str, object]:
        raw_body = self._app.current_event.body
        if raw_body in (None, ""):
            return {}
        try:
            return _JSON_OBJECT.validate_json(raw_body)
        except ValidationError as error:
            raise BadRequestError("The request body is not a JSON object") from error

    def _parameters(self, spec: OperationSpec) -> dict[str, object]:
        """Return each path, query, and header member sent, converted to its schema type."""
        sources = {
            ParameterLocation.PATH: dict(self._app.current_event.path_parameters or {}),
            ParameterLocation.QUERY: self._single_values(ParameterLocation.QUERY),
            ParameterLocation.HEADER: self._single_values(ParameterLocation.HEADER),
        }
        query_names = {binding.wire_name for binding in spec.parameters if binding.location is ParameterLocation.QUERY}
        unmodeled = sorted(sources[ParameterLocation.QUERY].keys() - query_names)
        if unmodeled:
            raise BadRequestError("A query parameter isn't modeled for the operation", fields=tuple(unmodeled))
        return {
            binding.member_name: self._converted(binding, sources[binding.location][binding.wire_name])
            for binding in spec.parameters
            if binding.wire_name in sources[binding.location]
        }

    def _single_values(self, location: ParameterLocation) -> dict[str, str]:
        """Return each query parameter or header by name (headers in lower case), rejecting a repeated one."""
        event = self._app.current_event
        single: Mapping[str, str] | None
        multi: Mapping[str, list[str]] | None
        if location is ParameterLocation.QUERY:
            single = event.query_string_parameters
            multi = event.multi_value_query_string_parameters
        else:
            single = _lower(event.headers)
            multi = _lower(event.multi_value_headers)
        repeated = sorted(name for name, values in (multi or {}).items() if len(values) != 1)
        if repeated:
            raise BadRequestError("A parameter was sent more than once", fields=tuple(repeated))
        return {**(single or {}), **{name: values[0] for name, values in (multi or {}).items()}}

    @staticmethod
    def _converted(binding: ParameterBinding, value: str) -> object:
        match binding.schema_type:
            case SchemaType.STRING:
                return value
            case SchemaType.INTEGER if value.lstrip("-").isdigit():
                return int(value)
            case SchemaType.NUMBER if _is_number(value):
                return float(value)
            case SchemaType.BOOLEAN if value in _BOOLEANS:
                return _BOOLEANS[value]
        raise BadRequestError("A parameter isn't its schema type", fields=(binding.member_name,))


def _lower[ValueT](values: Mapping[str, ValueT] | None) -> dict[str, ValueT] | None:
    return None if values is None else {name.lower(): value for name, value in values.items()}


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True
