"""Every DynamoDB call the DAOs make, on one table, reading and writing one Pydantic item model.

It owns the rules each DAO used to repeat: numbers are written as `Decimal` and read back as
`int` or `float`; the table contract's binary attributes are written as DynamoDB binary; key
attribute names come from the contract; an empty `ExpressionAttributeNames` or
`ExpressionAttributeValues` is never sent, in a single call or inside `TransactItems`; a
pagination token is the base64url JSON of `LastEvaluatedKey` and continues only the listing
that issued it; and every other AWS error becomes `DependencyThrottledError` or
`DependencyUnavailableError`.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from functools import reduce
from typing import TYPE_CHECKING, Any

from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from ccf_api_models.base import ValueModel
from ccf_api_models.contracts import AttributeType, IndexContract, KeyAttribute, TableContract
from pydantic import BaseModel, TypeAdapter, ValidationError

from common.aws_error_code import THROTTLING_CODES, AwsErrorCode
from common.errors import (
    BadRequestError,
    CancellationReason,
    FailedAwsCall,
    ResourceConflictError,
    ResourceNotFoundError,
    TransactionCanceledError,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.service_resource import Table

type AttributeValue = str | int | Decimal | bool | bytes | None | list[AttributeValue] | dict[str, AttributeValue]
type TransactWrite = dict[str, dict[str, Any]]
"""One `TransactItems` action, with Python values the DynamoDB resource's client serializes."""

_TOKEN: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])
_DESERIALIZER = TypeDeserializer()


class ItemKey(ValueModel):
    partition: str
    sort: str | Decimal | None = None


class SortOperator(StrEnum):
    EQUALS = "EQUALS"
    BEGINS_WITH = "BEGINS_WITH"


class SortCondition(ValueModel):
    operator: SortOperator
    value: str


class SortOrder(StrEnum):
    ASCENDING = "ASCENDING"
    DESCENDING = "DESCENDING"


class ReadConsistency(StrEnum):
    EVENTUAL = "EVENTUAL"
    STRONG = "STRONG"


class QueryRequest(ValueModel):
    partition_value: str
    index: str | None = None
    """An index name from the table contract; None queries the base table."""
    sort_condition: SortCondition | None = None
    filter_equals: Mapping[str, str] = {}
    """Attribute name to value, joined with AND into a `FilterExpression`."""
    max_results: int
    next_token: str | None = None
    order: SortOrder = SortOrder.ASCENDING
    consistency: ReadConsistency = ReadConsistency.EVENTUAL


class Page[ItemT: BaseModel](ValueModel):
    items: list[ItemT]
    next_token: str | None = None


class ListItemsRequest[ParentT](ValueModel):
    """One page of a DAO listing under a parent: a tenant, a destination, or a custom connector."""

    parent: ParentT
    max_results: int
    next_token: str | None = None
    filter_value: str | None = None
    """The operation's one filter query member: `name`, `status`, or `collection_id`."""


class OnConditionFailure(StrEnum):
    """`ReturnValuesOnConditionCheckFailure`, read only inside a transaction."""

    NONE = "NONE"
    ALL_OLD = "ALL_OLD"


class PutItemRequest[ItemT: BaseModel](ValueModel):
    item: ItemT
    action: str
    """What the DAO is doing, for example "Destination creation"; it names the error and holds no stored value."""
    condition: str | None = None


class UpdateItemRequest(ValueModel):
    key: ItemKey
    action: str
    expression: str
    condition: str | None = None
    values: Mapping[str, AttributeValue] = {}
    names: Mapping[str, str] = {}
    on_condition_failure: OnConditionFailure = OnConditionFailure.NONE


class DeleteItemRequest(ValueModel):
    key: ItemKey
    action: str
    condition: str | None = None
    values: Mapping[str, AttributeValue] = {}
    names: Mapping[str, str] = {}


class ConditionCheckRequest(ValueModel):
    key: ItemKey
    condition: str
    values: Mapping[str, AttributeValue] = {}
    names: Mapping[str, str] = {}
    on_condition_failure: OnConditionFailure = OnConditionFailure.NONE


class TransactionRequest(ValueModel):
    action: str
    writes: tuple[TransactWrite, ...]
    """Actions from `put_action`, `update_action`, `delete_action`, or `condition_check` of any `ModelTable`."""


class ItemAttributesRequest[ItemT: BaseModel](ValueModel):
    item: ItemT
    names: frozenset[str]


class ModelTable[ItemT: BaseModel]:
    def __init__(self, table: Table, client: DynamoDBClient, item_type: type[ItemT], contract: TableContract) -> None:
        self._table = table
        self._client = client
        self._item_type = item_type
        self._contract = contract

    def get(self, key: ItemKey) -> ItemT | None:
        """A strongly consistent read; None when there is no item."""
        try:
            item = self._table.get_item(Key=self._key(key), ConsistentRead=True).get("Item")
        except ClientError as error:
            raise FailedAwsCall(error=error, action=f"{self._contract.name} lookup").to_error() from error
        return None if item is None else self._model(item)

    def require(self, key: ItemKey) -> ItemT:
        item = self.get(key)
        if item is None:
            raise ResourceNotFoundError(f"The {self._contract.name} item does not exist")
        return item

    def put(self, request: PutItemRequest[ItemT]) -> ItemT:
        try:
            self._table.put_item(**self.put_action(request)["Put"])
        except ClientError as error:
            raise self._write_error(error, request.action) from error
        return request.item

    def update(self, request: UpdateItemRequest) -> ItemT:
        """Return the updated item; a failed condition re-reads it: no item is 404, any other is 409."""
        arguments = {**self.update_action(request)["Update"], "ReturnValues": "ALL_NEW"}
        arguments.pop("TableName")
        arguments.pop("ReturnValuesOnConditionCheckFailure", None)
        try:
            return self._model(self._table.update_item(**arguments)["Attributes"])
        except ClientError as error:
            if AwsErrorCode.from_client_error(error) is AwsErrorCode.CONDITIONAL_CHECK_FAILED:
                self.require(request.key)
            raise self._write_error(error, request.action) from error

    def delete(self, request: DeleteItemRequest) -> None:
        arguments = dict(self.delete_action(request)["Delete"])
        arguments.pop("TableName")
        try:
            self._table.delete_item(**arguments)
        except ClientError as error:
            raise self._write_error(error, request.action) from error

    def query(self, request: QueryRequest) -> Page[ItemT]:
        """One page of the listing; its token continues only this partition, sort condition, and index."""
        keys = self._contract if request.index is None else self._contract.indexes[request.index]
        condition: ConditionBase = Key(keys.partition_key.name).eq(request.partition_value)
        sort = request.sort_condition
        if sort is not None and keys.sort_key is not None:
            sort_key = Key(keys.sort_key.name)
            condition &= (
                sort_key.eq(sort.value) if sort.operator is SortOperator.EQUALS else sort_key.begins_with(sort.value)
            )
        arguments: dict[str, Any] = {
            "KeyConditionExpression": condition,
            "Limit": request.max_results,
            "ScanIndexForward": request.order is SortOrder.ASCENDING,
            "ConsistentRead": request.consistency is ReadConsistency.STRONG,
        }
        if request.index is not None:
            arguments["IndexName"] = request.index
        if request.filter_equals:
            filters: list[ConditionBase] = [Attr(name).eq(value) for name, value in request.filter_equals.items()]
            arguments["FilterExpression"] = reduce(lambda left, right: left & right, filters)
        if request.next_token is not None:
            arguments["ExclusiveStartKey"] = self._start_key(request)
        try:
            response = self._table.query(**arguments)
        except ClientError as error:
            raise FailedAwsCall(error=error, action=f"{self._contract.name} listing").to_error() from error
        last_key = response.get("LastEvaluatedKey")
        token = (
            None
            if not last_key
            else base64.urlsafe_b64encode(json.dumps(last_key, default=str, sort_keys=True).encode()).decode()
        )
        return Page(items=[self._model(item) for item in response["Items"]], next_token=token)

    def attributes(self, request: ItemAttributesRequest[ItemT]) -> dict[str, Any]:
        """The item's named members as the values an update writes: numbers as `Decimal`, binary members as bytes."""
        return {name: value for name, value in self._item(request.item).items() if name in request.names}

    def put_action(self, request: PutItemRequest[ItemT]) -> TransactWrite:
        put = {"TableName": self._contract.name, "Item": self._item(request.item), **_condition(request.condition)}
        return {"Put": put}

    def update_action(self, request: UpdateItemRequest) -> TransactWrite:
        update = {
            "TableName": self._contract.name,
            "Key": self._key(request.key),
            "UpdateExpression": request.expression,
            **_condition(request.condition),
            **_expression_maps(request),
            **_on_failure(request.on_condition_failure),
        }
        return {"Update": update}

    def delete_action(self, request: DeleteItemRequest) -> TransactWrite:
        delete = {
            "TableName": self._contract.name,
            "Key": self._key(request.key),
            **_condition(request.condition),
            **_expression_maps(request),
        }
        return {"Delete": delete}

    def condition_check(self, request: ConditionCheckRequest) -> TransactWrite:
        check = {
            "TableName": self._contract.name,
            "Key": self._key(request.key),
            "ConditionExpression": request.condition,
            **_expression_maps(request),
            **_on_failure(request.on_condition_failure),
        }
        return {"ConditionCheck": check}

    def transact(self, request: TransactionRequest) -> None:
        """Send the actions together; a canceled transaction raises its reasons in action order."""
        try:
            # The client is the DynamoDB resource's, which serializes Python values; its stubs type serialized ones.
            self._client.transact_write_items(TransactItems=list(request.writes))  # type: ignore[arg-type]
        except ClientError as error:
            codes = AwsErrorCode.ordered_cancellation_reasons(error)
            canceled = AwsErrorCode.from_client_error(error) is AwsErrorCode.TRANSACTION_CANCELED
            if not canceled or THROTTLING_CODES & set(codes):
                raise FailedAwsCall(error=error, action=request.action).to_error() from error
            reasons = tuple(
                CancellationReason(code=code, item=_old_item(raw))
                for code, raw in zip(codes, error.response.get("CancellationReasons", []), strict=True)
            )
            raise TransactionCanceledError(f"{request.action} was canceled", reasons=reasons) from error

    def _write_error(self, error: ClientError, action: str) -> Exception:
        if AwsErrorCode.from_client_error(error) is AwsErrorCode.CONDITIONAL_CHECK_FAILED:
            return ResourceConflictError(f"{action} conflicts with the item's state")
        return FailedAwsCall(error=error, action=action).to_error()

    def _key(self, key: ItemKey) -> dict[str, Any]:
        sort_key = self._contract.sort_key
        return {
            self._contract.partition_key.name: key.partition,
            **({} if sort_key is None else {sort_key.name: key.sort}),
        }

    def _item(self, item: ItemT) -> dict[str, Any]:
        binary = set(self._contract.binary_attributes)
        values = json.loads(item.model_dump_json(exclude_none=True, exclude=binary), parse_float=Decimal)
        return {**values, **item.model_dump(include=binary, exclude_none=True)}

    def _model(self, item: Mapping[str, Any]) -> ItemT:
        binary = self._contract.binary_attributes
        members = {name: value for name, value in item.items() if name not in binary}
        encoded = {name: base64.b64encode(bytes(item[name])).decode("ascii") for name in binary if name in item}
        return self._item_type.model_validate_json(json.dumps({**members, **encoded}, default=_number))

    def _start_key(self, request: QueryRequest) -> dict[str, str | Decimal]:
        """Decode the token, or raise BadRequestError when it belongs to another listing."""
        attributes = self._key_attributes(request.index)
        try:
            raw = _TOKEN.validate_json(base64.urlsafe_b64decode((request.next_token or "").encode()))
            key = {name: Decimal(raw[name]) if attributes[name] is AttributeType.NUMBER else raw[name] for name in raw}
        except (binascii.Error, ValueError, KeyError, ValidationError) as error:
            raise BadRequestError("The pagination token is invalid", fields=("next_token",)) from error
        keys = self._contract if request.index is None else self._contract.indexes[request.index]
        sort = request.sort_condition
        sort_value = None if keys.sort_key is None else str(key.get(keys.sort_key.name))
        valid = key.keys() == attributes.keys() and key[keys.partition_key.name] == request.partition_value
        if sort is not None and sort_value is not None:
            valid &= (
                sort_value == sort.value if sort.operator is SortOperator.EQUALS else sort_value.startswith(sort.value)
            )
        if not valid:
            raise BadRequestError("The pagination token is invalid", fields=("next_token",))
        return key

    def _key_attributes(self, index: str | None) -> dict[str, AttributeType]:
        """Every attribute of the listing's start key: the table's keys, and the index's keys."""
        schemas: list[TableContract | IndexContract] = [
            self._contract,
            *([] if index is None else [self._contract.indexes[index]]),
        ]
        attributes: list[KeyAttribute] = [
            key for schema in schemas for key in (schema.partition_key, schema.sort_key) if key is not None
        ]
        return {attribute.name: attribute.type for attribute in attributes}


def _condition(condition: str | None) -> dict[str, str]:
    return {} if condition is None else {"ConditionExpression": condition}


def _expression_maps(request: UpdateItemRequest | DeleteItemRequest | ConditionCheckRequest) -> dict[str, Any]:
    """The expression maps that aren't empty; DynamoDB rejects an empty one."""
    maps = {"ExpressionAttributeNames": dict(request.names), "ExpressionAttributeValues": dict(request.values)}
    return {member: values for member, values in maps.items() if values}


def _on_failure(on_failure: OnConditionFailure) -> dict[str, str]:
    return {} if on_failure is OnConditionFailure.NONE else {"ReturnValuesOnConditionCheckFailure": on_failure.value}


def _old_item(reason: Mapping[str, Any]) -> Mapping[str, object] | None:
    """The item a failed condition saw, from the error response's DynamoDB JSON."""
    item: Mapping[str, Any] | None = reason.get("Item")
    return None if item is None else {name: _DESERIALIZER.deserialize(value) for name, value in item.items()}


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)
