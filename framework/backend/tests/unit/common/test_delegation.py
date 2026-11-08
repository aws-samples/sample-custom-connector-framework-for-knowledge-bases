"""Who may call each operation: tenant principals, runtime roles, and the delegation members."""

from __future__ import annotations

from typing import Any, cast

import pytest
from ccf_api_models.base import CcfModel
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, Partition, TenantContext
from ccf_api_models.models import (
    ContainerProperties,
    CreateCustomConnectorRequest,
    ResourceRequirements,
    SourceAccessInput,
)
from ccf_api_models.operations import Operation

from common.authenticated_call import AuthenticatedCall
from common.delegation import TenantResolver
from common.errors import UnauthorizedCallerError
from common.operation import ResolvedCaller, ResolveTenantRequest
from common.resource_ids import SystemClock
from common.settings import ApiSettings, RuntimeRoleSettings
from common.storage.ddb.custom_connectors_dao import CreateConnectorRecord, CustomConnectorsDao, CustomConnectorsTables

CATALOG = OperationCatalog.load()
TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
SERVICE_ACCOUNT = "444455556666"
DESTINATION = DestinationRef(tenant=TENANT, destination_id="dst-7c41e9a2b5d0")
OTHER_DESTINATION_ID = "dst-0a1b2c3d4e5f"
CONNECTOR = ConnectorRef(destination=DESTINATION, connector_id="cc-e020d65d76c0")
OTHER_CONNECTOR = ConnectorRef(
    destination=DestinationRef(tenant=TENANT, destination_id=OTHER_DESTINATION_ID), connector_id="cc-0a1b2c3d4e5f"
)
RUNTIME_ROLE = "CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b"
OTHER_RUNTIME_ROLE = "CcfConnectorRuntime-0000000000000000000000000000000a"
TENANT_PRINCIPAL = "arn:aws:sts::111122223333:assumed-role/Admin/session"
RUNTIME_CALLER = f"arn:aws:sts::444455556666:assumed-role/{RUNTIME_ROLE}/ccj-0fdef5d64630"
RUNTIME_ONLY = [
    Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS,
    Operation.BATCH_DELETE_CUSTOM_CONNECTOR_DOCUMENTS,
    Operation.BATCH_DELETE_CUSTOM_CONNECTOR_COLLECTIONS,
]


RUNTIME_ROLES = RuntimeRoleSettings.model_validate(
    {
        "rest_api_id": "abcdef1234",
        "stage_name": "ccf",
        "boundary_arn": "arn:aws:iam::444455556666:policy/ccf/CcfConnectorRuntimeBoundary",
        "routes": '[{"method":"GET","path":"/api/v1/destinations/{destination_id}"}]',
        "role_path": "/ccf-runtime/",
        "role_name_prefix": "CcfConnectorRuntime-",
        "policy_name": "CcfConnectorRuntimePolicy",
        "max_session_seconds": 43_200,
    }
)


def settings(allowed: tuple[str, ...] = ("111122223333",)) -> ApiSettings:
    return ApiSettings.model_construct(
        aws_region="us-east-1",
        aws_partition=Partition.AWS,
        service_account_id=SERVICE_ACCOUNT,
        allowed_tenant_account_ids=allowed,
        runtime_roles=RUNTIME_ROLES,
    )


def call(user_arn: str) -> AuthenticatedCall:
    account_id = user_arn.split(":")[4]
    return AuthenticatedCall.from_event(
        {"requestContext": {"identity": {"accountId": account_id, "userArn": user_arn}}}
    )


def store(dynamodb: Any, connectors: CustomConnectorsDao, connector: ConnectorRef, role_name: str) -> None:
    """Save an `ACTIVE` destination and a custom connector whose runtime role is `role_name`."""
    dynamodb.meta.client.put_item(
        TableName="CcfDestinations",
        Item={
            "tenant_arn_prefix": connector.tenant.arn_prefix,
            "destination_id": connector.destination_id,
            "status": "ACTIVE",
        },
    )
    payload = CreateCustomConnectorRequest(
        destination_id=connector.destination_id,
        name=f"github-{connector.connector_id}",
        container_properties=ContainerProperties(
            image_uri="111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:1.0.0",
            command=["ccf-connector"],
            resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
            timeout=3600,
        ),
        source_access=SourceAccessInput(role_arn="arn:aws:iam::111122223333:role/CcfSourceRole-github"),
    )
    connectors.create(
        CreateConnectorRecord(
            connector=connector,
            payload=payload,
            external_id="ccf-ext-2f9d41c7a8b04e1f9c3d5a6b7e8f9012",
            runtime_role_arn=f"arn:aws:iam::444455556666:role/ccf-runtime/{role_name}",
        )
    )


def connectors_dao(dynamodb: Any, table_names: Any) -> CustomConnectorsDao:
    return CustomConnectorsDao.over(
        CustomConnectorsTables(
            table=dynamodb.Table(table_names.connectors),
            destinations_table=dynamodb.Table(table_names.destinations),
            client=dynamodb.meta.client,
            clock=SystemClock(),
        )
    )


@pytest.fixture
def resolver(dynamodb: Any, table_names: Any) -> TenantResolver:
    connectors = connectors_dao(dynamodb, table_names)
    store(dynamodb, connectors, CONNECTOR, RUNTIME_ROLE)
    store(dynamodb, connectors, OTHER_CONNECTOR, OTHER_RUNTIME_ROLE)
    return TenantResolver(connectors, settings())


def constructed(model: type[CcfModel], members: dict[str, str | None]) -> CcfModel:
    """The request without validation; the resolver reads only the members given."""
    # The pydantic plugin types model_construct by the model's own fields, which CcfModel has none of.
    unvalidated: Any = model
    return cast(CcfModel, unvalidated.model_construct(**members))


def payload(operation: Operation, **members: str | None) -> CcfModel:
    """The operation's request with only the members that say whom it acts for; the rest aren't read."""
    spec = CATALOG.spec(operation)
    scope = {"destination_id": DESTINATION.destination_id, "connector_id": CONNECTOR.connector_id, **members}
    fields = spec.request_model.model_fields
    return constructed(
        spec.request_model, {name: value for name, value in scope.items() if name in fields and value is not None}
    )


def resolve(resolver: TenantResolver, operation: Operation, caller: str, **members: str | None) -> ResolvedCaller:
    return resolver.resolve(
        ResolveTenantRequest(
            call=call(caller), payload=payload(operation, **members), callers=CATALOG.spec(operation).callers
        )
    )


DELEGATED = {"tenant_arn_prefix": TENANT.arn_prefix}


@pytest.mark.parametrize("operation", RUNTIME_ONLY)
def test_a_tenant_principal_on_a_runtime_only_operation_is_refused(
    resolver: TenantResolver, operation: Operation
) -> None:
    with pytest.raises(UnauthorizedCallerError):
        resolve(resolver, operation, TENANT_PRINCIPAL)


@pytest.mark.parametrize(
    "operation",
    [
        Operation.DELETE_CUSTOM_CONNECTOR_CHECKPOINT,
        Operation.PUT_CUSTOM_CONNECTOR_CHECKPOINT,
        Operation.GET_CUSTOM_CONNECTOR_CHECKPOINT,
        Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS,
        Operation.LIST_CUSTOM_CONNECTOR_COLLECTIONS,
    ],
)
def test_a_tenant_principal_may_call_checkpoint_and_list_operations(
    resolver: TenantResolver, operation: Operation
) -> None:
    assert resolve(resolver, operation, TENANT_PRINCIPAL) == ResolvedCaller(tenant=TENANT)


def test_a_delegated_call_on_a_tenant_only_operation_is_refused(resolver: TenantResolver) -> None:
    with pytest.raises(UnauthorizedCallerError):
        resolve(resolver, Operation.DELETE_CUSTOM_CONNECTOR_CHECKPOINT, RUNTIME_CALLER, **DELEGATED)


def test_a_runtime_role_acts_for_its_own_custom_connector_and_returns_the_item_it_read(
    resolver: TenantResolver,
) -> None:
    resolved = resolve(resolver, Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS, RUNTIME_CALLER, **DELEGATED)

    assert resolved.tenant == TENANT
    assert resolved.connector is not None
    assert resolved.connector.connector_id == CONNECTOR.connector_id


def test_the_runtime_role_of_another_custom_connector_is_refused(resolver: TenantResolver) -> None:
    caller = f"arn:aws:sts::444455556666:assumed-role/{OTHER_RUNTIME_ROLE}/ccj-0fdef5d64630"

    with pytest.raises(UnauthorizedCallerError):
        resolve(resolver, Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS, caller, **DELEGATED)


def test_a_runtime_role_naming_another_tenants_prefix_is_refused(resolver: TenantResolver) -> None:
    other = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="999988887777").arn_prefix

    with pytest.raises(UnauthorizedCallerError):
        resolve(resolver, Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS, RUNTIME_CALLER, tenant_arn_prefix=other)


def test_a_runtime_role_on_a_destination_its_connector_isnt_in_is_refused(resolver: TenantResolver) -> None:
    with pytest.raises(UnauthorizedCallerError):
        resolve(
            resolver,
            Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS,
            RUNTIME_CALLER,
            destination_id=OTHER_DESTINATION_ID,
            **DELEGATED,
        )


@pytest.mark.parametrize(
    "operation", [Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS, Operation.BATCH_PUT_CUSTOM_CONNECTOR_DOCUMENTS]
)
def test_a_runtime_role_without_the_tenant_arn_prefix_is_refused(
    resolver: TenantResolver, operation: Operation
) -> None:
    with pytest.raises(UnauthorizedCallerError):
        resolve(resolver, operation, RUNTIME_CALLER)


def test_another_service_account_role_without_the_prefix_is_a_tenant_principal(resolver: TenantResolver) -> None:
    caller = "arn:aws:sts::444455556666:assumed-role/Operator/session"

    resolved = resolve(resolver, Operation.LIST_CUSTOM_CONNECTOR_DOCUMENTS, caller)

    assert resolved.tenant.account_id == SERVICE_ACCOUNT


def test_a_delegated_get_destination_names_its_custom_connector(resolver: TenantResolver) -> None:
    resolved = resolve(resolver, Operation.GET_DESTINATION, RUNTIME_CALLER, **DELEGATED)

    assert resolved.tenant == TENANT


@pytest.mark.parametrize("connector_id", [None, OTHER_CONNECTOR.connector_id])
def test_a_delegated_get_destination_without_its_connector_or_naming_another_destinations_is_refused(
    resolver: TenantResolver, connector_id: str | None
) -> None:
    request = ResolveTenantRequest(
        call=call(RUNTIME_CALLER),
        payload=constructed(
            CATALOG.spec(Operation.GET_DESTINATION).request_model,
            {"destination_id": DESTINATION.destination_id, "connector_id": connector_id, **DELEGATED},
        ),
        callers=CATALOG.spec(Operation.GET_DESTINATION).callers,
    )

    with pytest.raises(UnauthorizedCallerError):
        resolver.resolve(request)


def test_a_caller_from_an_account_that_isnt_allowed_is_refused(dynamodb: Any, table_names: Any) -> None:
    service_account_only = TenantResolver(connectors_dao(dynamodb, table_names), settings(allowed=()))

    with pytest.raises(UnauthorizedCallerError):
        resolve(service_account_only, Operation.LIST_DESTINATIONS, TENANT_PRINCIPAL)
