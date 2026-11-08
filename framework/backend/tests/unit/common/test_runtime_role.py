"""Runtime role routes, names, tags, and the IAM errors the lifecycle treats as done."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from ccf_api_models.contracts import RuntimeRoleTagsContract
from ccf_api_models.identifiers import ConnectorRef, DestinationRef, Partition, TenantContext

from common.errors import DependencyUnavailableError
from common.runtime_role import ConnectorRuntimeRoleLifecycle, RuntimeRoleAccess
from common.settings import RuntimeRoleSettings

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
SERVICE = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="444455556666")
CONNECTOR = ConnectorRef(
    destination=DestinationRef(tenant=TENANT, destination_id="dst-7c41e9a2b5d0"), connector_id="cc-e020d65d76c0"
)
ACCESS = RuntimeRoleAccess(connector=CONNECTOR, source_role_arn="arn:aws:iam::111122223333:role/CcfSourceRole-github")
SETTINGS = RuntimeRoleSettings.model_validate(
    {
        "rest_api_id": "abcdef1234",
        "stage_name": "ccf",
        "boundary_arn": "arn:aws:iam::444455556666:policy/ccf/CcfConnectorRuntimeBoundary",
        "routes": '[{"method":"GET",'
        '"path":"/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/jobs/{job_id}"}]',
        "role_path": "/ccf-runtime/",
        "role_name_prefix": "CcfConnectorRuntime-",
        "policy_name": "CcfConnectorRuntimePolicy",
        "max_session_seconds": 43_200,
    }
)


class FakeIam:
    """Records each IAM call by name, and raises the error code set for an action."""

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _call(self, action: str, arguments: dict[str, Any]) -> object:
        self.calls.append((action, arguments))
        code = self.failures.get(action)
        if code is not None:
            raise ClientError({"Error": {"Code": code, "Message": "no"}}, action)
        return {}

    def create_role(self, **arguments: Any) -> object:
        return self._call("CreateRole", arguments)

    def tag_role(self, **arguments: Any) -> object:
        return self._call("TagRole", arguments)

    def untag_role(self, **arguments: Any) -> object:
        return self._call("UntagRole", arguments)

    def put_role_policy(self, **arguments: Any) -> object:
        return self._call("PutRolePolicy", arguments)

    def delete_role_policy(self, **arguments: Any) -> object:
        return self._call("DeleteRolePolicy", arguments)

    def delete_role(self, **arguments: Any) -> object:
        return self._call("DeleteRole", arguments)

    def names(self) -> list[str]:
        return [action for action, _ in self.calls]


def lifecycle(iam: FakeIam) -> ConnectorRuntimeRoleLifecycle:
    return ConnectorRuntimeRoleLifecycle(iam, SETTINGS, SERVICE)


def test_a_route_fills_in_the_connectors_ids_and_leaves_other_members_as_wildcards() -> None:
    [route] = SETTINGS.routes

    assert route.resource_for(CONNECTOR) == (
        "GET/api/v1/destinations/dst-7c41e9a2b5d0/custom-connectors/cc-e020d65d76c0/jobs/*"
    )


def test_the_role_name_of_a_known_connector_is_the_one_existing_roles_have() -> None:
    assert lifecycle(FakeIam()).role_name(CONNECTOR) == "CcfConnectorRuntime-573be2c23f30942ec135cf64f4def024"


def test_create_names_the_role_by_the_settings_and_tags_it_with_the_contracts_keys() -> None:
    iam = FakeIam()

    role_arn = lifecycle(iam).create(ACCESS)

    [(_, created)] = [call for call in iam.calls if call[0] == "CreateRole"]
    keys = RuntimeRoleTagsContract.load()
    assert (created["Path"], created["MaxSessionDuration"]) == ("/ccf-runtime/", 43_200)
    assert {tag["Key"] for tag in created["Tags"]} == {
        keys.destination_id,
        keys.connector_id,
        keys.tenant_account,
        keys.source_access_role,
    }
    assert role_arn == f"arn:aws:iam::444455556666:role/ccf-runtime/{created['RoleName']}"


def test_creating_a_role_that_already_exists_rewrites_its_policy() -> None:
    iam = FakeIam({"CreateRole": "EntityAlreadyExists"})

    lifecycle(iam).create(ACCESS)

    assert iam.names() == ["CreateRole", "TagRole", "UntagRole", "PutRolePolicy"]


def test_deleting_a_role_that_is_already_gone_succeeds() -> None:
    iam = FakeIam({"DeleteRolePolicy": "NoSuchEntity", "DeleteRole": "NoSuchEntity"})

    lifecycle(iam).delete(CONNECTOR)

    assert iam.names() == ["DeleteRolePolicy", "DeleteRole"]


def test_a_failed_iam_call_raises_the_dependency_error_naming_the_action() -> None:
    iam = FakeIam({"PutRolePolicy": "ServiceFailure"})

    with pytest.raises(DependencyUnavailableError, match="Runtime role policy update"):
        lifecycle(iam).create(ACCESS)
