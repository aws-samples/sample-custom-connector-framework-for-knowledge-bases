"""Client-token idempotency for the operations that take a client token, backed by the `CcfIdempotency` table.

Powertools stores one record per key, a hash of the tenant, operation, and client token, so two
tenants that send the same token create two resources. The record keeps a hash of the request:
the same token with another request raises `IdempotencyValidationError`, and a call while the
first is still running raises `IdempotencyAlreadyInProgressError`. A first call that raises
deletes its record, so a retry creates the resource. A request without a client token runs
without a record.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

import boto3
from aws_lambda_powertools.utilities.idempotency import (
    BasePersistenceLayer,
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.idempotency.serialization.base import BaseIdempotencySerializer
from aws_lambda_powertools.utilities.typing import LambdaContext
from ccf_api_models.base import CcfModel, ValueModel
from ccf_api_models.catalog import HeaderMember, OperationSpec
from ccf_api_models.operations import Operation
from pydantic import BaseModel, ConfigDict, field_serializer

from common.operation import Activity, OperationRequest
from common.settings import IdempotencySettings, TableNames

_UNHASHED_MEMBERS = frozenset({"client_token", *HeaderMember})
"""The members a retry may change without being another call."""


class IdempotencyKey(ValueModel):
    tenant_arn_prefix: str
    operation: Operation
    client_token: str


class IdempotentCreateRequest[RequestT: CcfModel](BaseModel):
    """What Powertools keys on (`key`) and hashes to tell a retry from another call (`request`)."""

    model_config = ConfigDict(frozen=True)

    key: IdempotencyKey | None
    request: RequestT

    @field_serializer("request")
    def _request_digest(self, request: RequestT) -> str:
        """Hash the request, so the stored record holds no request value."""
        values = {name: value for name, value in request.model_dump_for_wire().items() if name not in _UNHASHED_MEMBERS}
        wire = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(wire.encode("utf-8")).hexdigest()


class IdempotentRunRequest(ValueModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: OperationSpec
    request: OperationRequest[Any]
    activity: Activity


class IdempotentRoot(Protocol):
    """The settings of a root that runs creates: `ApiSettings` and `ScheduledStartSettings`."""

    @property
    def tables(self) -> TableNames: ...

    @property
    def idempotency(self) -> IdempotencySettings: ...


class _StrictModelSerializer(BaseIdempotencySerializer):
    """Reads a saved response back in JSON mode.

    Powertools' own Pydantic serializer validates the saved dict in Python mode, which the strict
    generated models reject: an enum or a number saved as JSON isn't the Python value they require.
    """

    def __init__(self, model: type[CcfModel]) -> None:
        self._model = model

    def to_dict(self, data: CcfModel) -> dict[str, Any]:
        return data.model_dump(mode="json")

    def from_dict(self, data: dict[str, Any]) -> CcfModel:
        return self._model.model_validate_json(json.dumps(data))


class IdempotentCreates:
    """Runs an activity at most once per tenant, operation, and client token."""

    def __init__(self, persistence: BasePersistenceLayer, config: IdempotencyConfig) -> None:
        self._persistence = persistence
        self._config = config

    @classmethod
    def from_settings(cls, settings: IdempotentRoot) -> IdempotentCreates:
        """Build the root's persistence layer and config once: the key, the hashed request, and the expiry."""
        return cls(
            DynamoDBPersistenceLayer(table_name=settings.tables.idempotency, boto3_client=boto3.client("dynamodb")),
            IdempotencyConfig(
                event_key_jmespath="key",
                payload_validation_jmespath="request",
                expires_after_seconds=settings.idempotency.expiry_seconds,
                raise_on_no_idempotency_key=True,
            ),
        )

    def register_lambda_context(self, context: LambdaContext) -> None:
        """Let Powertools expire an in-progress record when the invocation runs out of time."""
        self._config.register_lambda_context(context)

    def run(self, request: IdempotentRunRequest) -> CcfModel | None:
        """Return the activity's response, or the saved response of an earlier call with the same key and request."""
        spec, call = request.spec, request.request
        client_token = None if spec.client_token_member is None else getattr(call.payload, spec.client_token_member)
        if client_token is None or spec.response_model is None:
            return request.activity(call)
        key = IdempotencyKey(
            tenant_arn_prefix=call.tenant.arn_prefix, operation=spec.operation, client_token=client_token
        )

        def create(create_request: IdempotentCreateRequest[CcfModel]) -> CcfModel | None:
            return request.activity(call)

        guarded = idempotent_function(
            data_keyword_argument="create_request",
            persistence_store=self._persistence,
            config=self._config,
            output_serializer=_StrictModelSerializer(spec.response_model),
        )(create)
        saved: CcfModel = guarded(create_request=IdempotentCreateRequest(key=key, request=call.payload))
        return saved
