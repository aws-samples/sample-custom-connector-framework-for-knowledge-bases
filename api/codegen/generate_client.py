from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
_REQUEST_MODEL_EXTENSION = "x-ccf-request-model"
_MEMBER_NAME_EXTENSION = "x-ccf-member-name"
_CLIENT_SOURCE = (
    '"""PRIVATE generated CCF client operations. DO NOT EDIT."""\n'
    "# ruff: noqa: E501\n\nfrom __future__ import annotations\n\n"
    "from dataclasses import dataclass\nfrom typing import cast\n\n"
    "from ccf_api_models.base import CcfModel\nfrom ccf_api_models.models import (\n{imports}\n)\n\n\n"
    "@dataclass(frozen=True)\nclass _Operation:\n"
    '    """Generated HTTP bindings consumed by the private transport."""\n\n'
    "    python_name: str\n    method: str\n    path: str\n    success_status: int\n"
    "    request_model: type[CcfModel]\n    response_model: type[CcfModel] | None\n"
    "    path_bindings: tuple[tuple[str, str], ...]\n"
    "    query_bindings: tuple[tuple[str, str], ...]\n    body_members: tuple[str, ...]\n\n\n"
    "GENERATED_OPERATIONS: tuple[_Operation, ...] = (\n{metadata}\n)\n\n\n"
    "class _GeneratedCcfOperations:\n"
    '    """Generated typed operation surface mixed into the sole public client."""\n\n'
    "    def _invoke(self, operation: _Operation, request: CcfModel) -> CcfModel | None:\n"
    "        raise NotImplementedError\n\n{methods}\n\n\n__all__: list[str] = []\n"
)

type Binding = tuple[str, str]
type InventoryEntry = tuple[str, str, str, dict[str, Any], list[Any]]


class ClientGenerator:
    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    def generate(self) -> tuple[str, int]:
        inventory = self._read_inventory()
        rendered = [self._render_operation(index, entry) for index, entry in enumerate(inventory)]
        source = _CLIENT_SOURCE.format(
            imports="\n".join(f"    {name}," for name in sorted(set().union(*(part[0] for part in rendered)))),
            metadata="\n".join(part[1] for part in rendered),
            methods="\n\n".join(part[2] for part in rendered),
        )
        return source, len(inventory)

    def _read_inventory(self) -> list[InventoryEntry]:
        inventory: list[InventoryEntry] = []
        smithy_names: list[str] = []
        for path, path_item_value in self._document.get("paths", {}).items():
            if not isinstance(path_item_value, dict):
                continue
            inherited = cast(list[Any], path_item_value.get("parameters", []))
            for method, operation_value in path_item_value.items():
                if method not in _HTTP_METHODS or not isinstance(operation_value, dict):
                    continue
                smithy_name = self._required_string(operation_value, "operationId")
                smithy_names.append(smithy_name)
                inventory.append((self._snake_case(smithy_name), path, method, operation_value, inherited))
        python_names = [entry[0] for entry in inventory]
        if len(smithy_names) != len(set(smithy_names)):
            raise ValueError("duplicate Smithy operation names")
        if len(python_names) != len(set(python_names)):
            raise ValueError("duplicate generated Python operation names")
        return sorted(inventory)

    def _render_operation(self, index: int, entry: InventoryEntry) -> tuple[set[str], str, str]:
        python_name, path, method, operation, inherited = entry
        request_model = self._required_string(operation, _REQUEST_MODEL_EXTENSION)
        parameters = [*inherited, *cast(list[Any], operation.get("parameters", []))]
        path_bindings, query_bindings = self._read_bindings(parameters)
        placeholders = set(re.findall(r"\{([^{}]+)\}", path))
        wire_names = {wire_name for _, wire_name in path_bindings}
        if placeholders != wire_names:
            raise ValueError(f"path placeholders {sorted(placeholders)} do not match bindings {sorted(wire_names)}")
        schema = cast(dict[str, Any], self._document["components"]["schemas"]).get(request_model)
        if not isinstance(schema, dict):
            raise ValueError(f"request schema is missing: {request_model}")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"request schema has no properties: {request_model}")
        parameter_members = {name for name, _ in (*path_bindings, *query_bindings)}
        body_members = tuple(sorted(properties.keys() - parameter_members))
        success_status, response_model = self._read_success(operation)
        response = response_model or "None"
        operation_metadata = (
            "    _Operation(\n"
            f"        {python_name!r}, {method.upper()!r}, {path!r}, {success_status},\n"
            f"        {request_model}, {response}, {path_bindings!r}, {query_bindings!r}, {body_members!r},\n"
            "    ),"
        )
        signature = f"    def {python_name}(self, request: {request_model}) -> {response}:"
        invocation = f"self._invoke(GENERATED_OPERATIONS[{index}], request)"
        statement = invocation if response_model is None else f"return cast({response}, {invocation})"
        return (
            {request_model, *([response_model] if response_model else [])},
            operation_metadata,
            f"{signature}\n        {statement}",
        )

    def _read_bindings(self, parameters: list[Any]) -> tuple[tuple[Binding, ...], tuple[Binding, ...]]:
        bindings: dict[str, list[Binding]] = {"path": [], "query": []}
        for parameter_value in parameters:
            parameter = self._resolve(parameter_value)
            location = self._required_string(parameter, "in")
            if location not in bindings:
                raise ValueError(f"unsupported client parameter binding: {location}")
            wire_name = self._required_string(parameter, "name")
            bindings[location].append((str(parameter.get(_MEMBER_NAME_EXTENSION, wire_name)), wire_name))
        return tuple(bindings["path"]), tuple(bindings["query"])

    def _read_success(self, operation: dict[str, Any]) -> tuple[int, str | None]:
        responses = operation.get("responses")
        if not isinstance(responses, dict):
            raise ValueError("operation responses must be an object")
        statuses = [status for status in responses if str(status).startswith("2")]
        if len(statuses) != 1:
            raise ValueError(f"operation must have exactly one success status: {statuses}")
        status = statuses[0]
        content = self._resolve(responses[status]).get("content")
        if content is None:
            return int(status), None
        if not isinstance(content, dict) or "application/json" not in content:
            raise ValueError("success response must use application/json")
        schema = self._resolve(content["application/json"]).get("schema")
        reference = schema.get("$ref") if isinstance(schema, dict) else None
        if not isinstance(reference, str) or not reference.startswith("#/components/schemas/"):
            raise ValueError("success response must reference one generated model")
        return int(status), reference.rsplit("/", maxsplit=1)[-1]

    def _resolve(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("OpenAPI contract value must be an object")
        reference = value.get("$ref")
        if reference is None:
            return value
        if not isinstance(reference, str) or not reference.startswith("#/components/"):
            raise ValueError(f"unsupported reference: {reference}")
        resolved: Any = self._document
        for segment in reference.removeprefix("#/").split("/"):
            resolved = resolved[segment]
        if not isinstance(resolved, dict):
            raise ValueError(f"reference does not resolve to an object: {reference}")
        return resolved

    @staticmethod
    def _required_string(value: dict[str, Any], key: str) -> str:
        member = value.get(key)
        if not isinstance(member, str) or not member:
            raise ValueError(f"{key} must be a non-empty string")
        return member

    @staticmethod
    def _snake_case(value: str) -> str:
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)).lower()


def _load_json(path: Path) -> dict[str, Any]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"expected an object in {path}")
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate typed CCF client operations from enriched OpenAPI.")
    parser.add_argument("--openapi", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    try:
        source, operation_count = ClientGenerator(_load_json(arguments.openapi)).generate()
        expected = source.encode()
        if arguments.check:
            if not arguments.output.exists():
                raise ValueError(f"generated client is missing: {arguments.output}")
            if arguments.output.read_bytes() != expected:
                raise ValueError(f"generated client is stale: {arguments.output}; run make -C api client")
        else:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_bytes(expected)
    except (KeyError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    action = "verified" if arguments.check else "generated"
    print(f"{action}: {operation_count} typed CCF client operations in {arguments.output}")


if __name__ == "__main__":
    main()
