"""Per-connector runtime role scoping and lifecycle tests."""

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from common.runtime_role import (
    RUNTIME_ROLE_PATH,
    RUNTIME_ROLE_POLICY_NAME,
    ConnectorRuntimeRoleLifecycle,
    RuntimeRoleDeployment,
    RuntimeRoleError,
    RuntimeRoleIdentity,
    RuntimeRolePolicyRequest,
    RuntimeRoute,
    create_object_prefix,
    parse_runtime_routes,
)
from common.tenant import TenantContext

_FRAMEWORK_ACCOUNT_ID = "210987654321"
_REST_API_ID = "abc123def4"
_RUNTIME_ROUTES = (
    RuntimeRoute(method="GET", path="/api/v1/custom-connectors/{connector_id}"),
    RuntimeRoute(method="GET", path="/api/v1/custom-connectors/{connector_id}/jobs/{job_id}"),
    RuntimeRoute(method="POST", path="/api/v1/custom-connectors/{connector_id}/documents"),
)
_TENANT = TenantContext("123456789012", "us-east-1")
_OTHER_TENANT = TenantContext("999999999999", "us-east-1")


class FakeIamClient:
    """Records the exact IAM calls one runtime-role lifecycle makes."""

    def __init__(self, errors: dict[str, ClientError] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._errors = errors or {}

    def create_role(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("create_role", kwargs)

    def put_role_policy(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("put_role_policy", kwargs)

    def delete_role_policy(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("delete_role_policy", kwargs)

    def delete_role(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("delete_role", kwargs)

    def _record(self, operation: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, kwargs))
        error = self._errors.get(operation)
        if error is not None:
            raise error
        return {}

    def policy_document(self) -> dict[str, Any]:
        document = next(kwargs["PolicyDocument"] for operation, kwargs in self.calls if operation == "put_role_policy")
        return dict(json.loads(document))


def deployment() -> RuntimeRoleDeployment:
    return RuntimeRoleDeployment(
        partition="aws",
        account_id=_FRAMEWORK_ACCOUNT_ID,
        region="us-east-1",
        ingestion_bucket_name="ccf-ingestion",
        destination_key_arn=f"arn:aws:kms:us-east-1:{_FRAMEWORK_ACCOUNT_ID}:key/key-id",
        permissions_boundary_arn=f"arn:aws:iam::{_FRAMEWORK_ACCOUNT_ID}:policy/CcfConnectorRuntimeBoundary",
        runtime_routes=_RUNTIME_ROUTES,
    )


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Iam")


def test_runtime_role_grants_only_one_connectors_api_prefix_and_source_role() -> None:
    iam_client = FakeIamClient()
    lifecycle = ConnectorRuntimeRoleLifecycle(iam_client, deployment())

    role_arn = lifecycle.create(
        RuntimeRolePolicyRequest(
            identity=RuntimeRoleIdentity(_TENANT, "cc-1"),
            source_role_arn="arn:aws:iam::123456789012:role/source-one",
            rest_api_id=_REST_API_ID,
        )
    )

    role_name = ConnectorRuntimeRoleLifecycle.role_name(RuntimeRoleIdentity(_TENANT, "cc-1"))
    assert role_arn == f"arn:aws:iam::{_FRAMEWORK_ACCOUNT_ID}:role{RUNTIME_ROLE_PATH}{role_name}"
    create_kwargs = iam_client.calls[0][1]
    assert create_kwargs["Path"] == RUNTIME_ROLE_PATH
    assert create_kwargs["PermissionsBoundary"] == deployment().permissions_boundary_arn
    trust_statement = json.loads(create_kwargs["AssumeRolePolicyDocument"])["Statement"][0]
    assert trust_statement["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert trust_statement["Condition"]["StringEquals"]["aws:SourceAccount"] == _FRAMEWORK_ACCOUNT_ID

    statements = {statement["Sid"]: statement for statement in iam_client.policy_document()["Statement"]}
    # Exactly the generated route templates, bound to this connector, with any
    # other path placeholder reduced to a wildcard.
    assert statements["ConnectorApiResources"]["Resource"] == [
        f"arn:aws:execute-api:us-east-1:{_FRAMEWORK_ACCOUNT_ID}:abc123def4/*/GET/api/v1/custom-connectors/cc-1",
        f"arn:aws:execute-api:us-east-1:{_FRAMEWORK_ACCOUNT_ID}:abc123def4/*/GET/api/v1/custom-connectors/cc-1/jobs/*",
        f"arn:aws:execute-api:us-east-1:{_FRAMEWORK_ACCOUNT_ID}:abc123def4/*/POST/api/v1/custom-connectors/cc-1/documents",
    ]
    assert statements["ConnectorDestinationPrefix"]["Resource"] == [
        "arn:aws:s3:::ccf-ingestion/documents/123456789012/cc-1/*"
    ]
    assert statements["ConnectorDestinationPrefix"]["Action"] == ["s3:PutObject", "s3:DeleteObject"]
    assert statements["ConnectorSourceRole"]["Resource"] == ["arn:aws:iam::123456789012:role/source-one"]
    assert statements["ConnectorDestinationEncryption"]["Condition"]["StringEquals"] == {
        "kms:ViaService": "s3.us-east-1.amazonaws.com"
    }
    granted_actions = {
        action
        for statement in iam_client.policy_document()["Statement"]
        for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    }
    assert granted_actions == {
        "execute-api:Invoke",
        "s3:PutObject",
        "s3:DeleteObject",
        "kms:GenerateDataKey",
        "kms:Decrypt",
        "sts:AssumeRole",
    }


def test_two_connectors_receive_distinct_roles_prefixes_and_source_grants() -> None:
    first = FakeIamClient()
    second = FakeIamClient()
    first_identity = RuntimeRoleIdentity(_TENANT, "cc-1")
    second_identity = RuntimeRoleIdentity(_TENANT, "cc-2")

    first_arn = ConnectorRuntimeRoleLifecycle(first, deployment()).create(
        RuntimeRolePolicyRequest(
            identity=first_identity,
            source_role_arn="arn:aws:iam::123456789012:role/source-one",
            rest_api_id=_REST_API_ID,
        )
    )
    second_arn = ConnectorRuntimeRoleLifecycle(second, deployment()).create(
        RuntimeRolePolicyRequest(
            identity=second_identity,
            source_role_arn="arn:aws:iam::123456789012:role/source-two",
            rest_api_id=_REST_API_ID,
        )
    )

    assert first_arn != second_arn
    first_document = json.dumps(first.policy_document())
    second_document = json.dumps(second.policy_document())
    assert "documents/123456789012/cc-1/*" in first_document
    assert "cc-2" not in first_document
    assert "documents/123456789012/cc-2/*" in second_document
    assert "cc-1" not in second_document
    assert "source-two" not in first_document
    assert "source-one" not in second_document


def test_runtime_role_names_separate_tenants_with_the_same_connector_id() -> None:
    identity = RuntimeRoleIdentity(_TENANT, "cc-1")
    other_identity = RuntimeRoleIdentity(_OTHER_TENANT, "cc-1")

    assert ConnectorRuntimeRoleLifecycle.role_name(identity) != ConnectorRuntimeRoleLifecycle.role_name(other_identity)
    assert create_object_prefix(_TENANT, "cc-1") != create_object_prefix(_OTHER_TENANT, "cc-1")
    assert create_object_prefix(_TENANT, "cc-1") == "documents/123456789012/cc-1/"


def test_existing_runtime_role_is_rescoped_rather_than_duplicated() -> None:
    iam_client = FakeIamClient({"create_role": client_error("EntityAlreadyExists")})
    lifecycle = ConnectorRuntimeRoleLifecycle(iam_client, deployment())

    lifecycle.create(
        RuntimeRolePolicyRequest(
            identity=RuntimeRoleIdentity(_TENANT, "cc-1"),
            source_role_arn="arn:aws:iam::123456789012:role/source-one",
            rest_api_id=_REST_API_ID,
        )
    )

    assert [operation for operation, _ in iam_client.calls] == ["create_role", "put_role_policy"]


def test_runtime_role_creation_failure_is_translated() -> None:
    iam_client = FakeIamClient({"create_role": client_error("LimitExceeded")})
    lifecycle = ConnectorRuntimeRoleLifecycle(iam_client, deployment())

    with pytest.raises(RuntimeRoleError, match="creation failed"):
        lifecycle.create(
            RuntimeRolePolicyRequest(
                identity=RuntimeRoleIdentity(_TENANT, "cc-1"),
                source_role_arn="arn:aws:iam::123456789012:role/source-one",
                rest_api_id=_REST_API_ID,
            )
        )


def test_runtime_role_deletion_is_idempotent_and_removes_the_inline_policy() -> None:
    iam_client = FakeIamClient(
        {
            "delete_role_policy": client_error("NoSuchEntity"),
            "delete_role": client_error("NoSuchEntity"),
        }
    )
    lifecycle = ConnectorRuntimeRoleLifecycle(iam_client, deployment())

    lifecycle.delete(RuntimeRoleIdentity(_TENANT, "cc-1"))

    assert [operation for operation, _ in iam_client.calls] == ["delete_role_policy", "delete_role"]
    assert iam_client.calls[0][1]["PolicyName"] == RUNTIME_ROLE_POLICY_NAME


def test_incomplete_runtime_role_deployment_configuration_fails_fast() -> None:
    with pytest.raises(RuntimeRoleError, match="incomplete"):
        RuntimeRoleDeployment(
            partition="aws",
            account_id=_FRAMEWORK_ACCOUNT_ID,
            region="us-east-1",
            ingestion_bucket_name="",
            destination_key_arn="arn:aws:kms:us-east-1:210987654321:key/key-id",
            permissions_boundary_arn="arn:aws:iam::210987654321:policy/CcfConnectorRuntimeBoundary",
            runtime_routes=_RUNTIME_ROUTES,
        )


def test_policy_failure_deletes_a_role_created_in_the_same_call() -> None:
    iam_client = FakeIamClient({"put_role_policy": client_error("MalformedPolicyDocument")})
    lifecycle = ConnectorRuntimeRoleLifecycle(iam_client, deployment())

    with pytest.raises(RuntimeRoleError, match="policy update failed"):
        lifecycle.create(
            RuntimeRolePolicyRequest(
                identity=RuntimeRoleIdentity(_TENANT, "cc-1"),
                source_role_arn="arn:aws:iam::123456789012:role/CcfSourceRole-one",
                rest_api_id=_REST_API_ID,
            )
        )

    # A role left without its scoping policy would be an unusable identity.
    assert [operation for operation, _ in iam_client.calls] == [
        "create_role",
        "put_role_policy",
        "delete_role_policy",
        "delete_role",
    ]
    role_name = ConnectorRuntimeRoleLifecycle.role_name(RuntimeRoleIdentity(_TENANT, "cc-1"))
    assert iam_client.calls[-1][1]["RoleName"] == role_name


def test_policy_failure_keeps_a_pre_existing_idempotent_role() -> None:
    iam_client = FakeIamClient(
        {
            "create_role": client_error("EntityAlreadyExists"),
            "put_role_policy": client_error("MalformedPolicyDocument"),
        }
    )
    lifecycle = ConnectorRuntimeRoleLifecycle(iam_client, deployment())

    with pytest.raises(RuntimeRoleError, match="policy update failed"):
        lifecycle.create(
            RuntimeRolePolicyRequest(
                identity=RuntimeRoleIdentity(_TENANT, "cc-1"),
                source_role_arn="arn:aws:iam::123456789012:role/CcfSourceRole-one",
                rest_api_id=_REST_API_ID,
            )
        )

    # The pre-existing role still carries its previous policy and must survive.
    assert [operation for operation, _ in iam_client.calls] == ["create_role", "put_role_policy"]


def test_runtime_routes_come_from_generated_metadata() -> None:
    routes = parse_runtime_routes(
        '[{"method":"GET","path":"/api/v1/custom-connectors/{connector_id}"},'
        '{"method":"DELETE","path":"/api/v1/custom-connectors/{connector_id}/jobs/{job_id}"}]'
    )

    assert [route.for_connector("cc-7") for route in routes] == [
        "GET/api/v1/custom-connectors/cc-7",
        "DELETE/api/v1/custom-connectors/cc-7/jobs/*",
    ]


@pytest.mark.parametrize(
    "serialized_routes",
    [
        "",
        "{}",
        "[]",
        '[{"method":"GET","path":"/api/v1/other/{connector_id}"}]',
        '[{"method":"GET","path":"/api/v1/custom-connectors"}]',
        '[{"method":"get","path":"/api/v1/custom-connectors/{connector_id}"}]',
        '[{"method":"GET","path":"/api/v1/custom-connectors/{connector_id}"},'
        '{"method":"GET","path":"/api/v1/custom-connectors/{connector_id}"}]',
    ],
    ids=["empty", "not-a-list", "no-routes", "wrong-prefix", "not-connector-scoped", "lowercase", "duplicate"],
)
def test_invalid_generated_route_metadata_fails_closed(serialized_routes: str) -> None:
    with pytest.raises(RuntimeRoleError):
        parse_runtime_routes(serialized_routes)
