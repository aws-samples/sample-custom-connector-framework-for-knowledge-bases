"""Service-managed per-connector runtime role lifecycle in the framework account.

The AWS Batch execution role pulls the client image and publishes job logs and is
never exposed to connector code. Each connector additionally receives one
service-managed runtime role whose inline policy reaches only that connector's
API resources, its service-authored destination prefix, and its exact registered
source role. Connector containers receive only runtime-role credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from botocore.exceptions import ClientError

from common.tenant import TenantContext

RUNTIME_ROLE_PATH = "/ccf-runtime/"
RUNTIME_ROLE_NAME_PREFIX = "CcfConnectorRuntime-"
RUNTIME_ROLE_POLICY_NAME = "CcfConnectorRuntimeAccess"
_RUNTIME_ROLE_MAX_SESSION_SECONDS = 43_200
_IDENTITY_HASH_LENGTH = 32
_ALREADY_EXISTS_CODES = frozenset({"EntityAlreadyExists"})
_MISSING_CODES = frozenset({"NoSuchEntity"})
_REST_API_ID = re.compile(r"^[A-Za-z0-9]{1,128}$")
_HTTP_METHOD = re.compile(r"^[A-Z]{3,7}$")
_ROUTE_PATH = re.compile(r"^/api/v1/custom-connectors/\{connector_id\}(?:/[A-Za-z0-9{}_-]+)*$")
_PLACEHOLDER = re.compile(r"\{[A-Za-z0-9_]+\}")
CONNECTOR_ID_PLACEHOLDER = "{connector_id}"


class RuntimeRoleIamClient(Protocol):
    """The exact IAM operations used to manage one connector runtime role."""

    def create_role(self, **kwargs: Any) -> Mapping[str, Any]:
        """Create a path-scoped, boundary-constrained runtime role."""
        ...

    def put_role_policy(self, **kwargs: Any) -> Mapping[str, Any]:
        """Replace the single inline policy of one runtime role."""
        ...

    def delete_role_policy(self, **kwargs: Any) -> Mapping[str, Any]:
        """Delete the single inline policy of one runtime role."""
        ...

    def delete_role(self, **kwargs: Any) -> Mapping[str, Any]:
        """Delete one runtime role."""
        ...


class RuntimeRoleError(RuntimeError):
    """Raised when a connector runtime role cannot be managed safely."""


@dataclass(frozen=True)
class RuntimeRoute:
    """One generated, connector-scoped route a connector runtime may call."""

    method: str
    path: str

    def __post_init__(self) -> None:
        if _HTTP_METHOD.fullmatch(self.method) is None or _ROUTE_PATH.fullmatch(self.path) is None:
            raise RuntimeRoleError("Generated runtime route metadata is invalid")

    def for_connector(self, connector_id: str) -> str:
        """Return the exact route suffix for one connector."""
        bound = self.path.replace(CONNECTOR_ID_PLACEHOLDER, connector_id)
        return f"{self.method}{_PLACEHOLDER.sub('*', bound)}"


def parse_runtime_routes(serialized_routes: str) -> tuple[RuntimeRoute, ...]:
    """Parse the deployment's generated runtime-callable route templates."""
    try:
        decoded: Any = json.loads(serialized_routes)
    except json.JSONDecodeError as error:
        raise RuntimeRoleError("Generated runtime route metadata is not valid JSON") from error
    if not isinstance(decoded, list) or not decoded:
        raise RuntimeRoleError("Generated runtime route metadata is empty")
    routes: list[RuntimeRoute] = []
    for entry in decoded:
        if not isinstance(entry, dict):
            raise RuntimeRoleError("Generated runtime route metadata is invalid")
        route = cast(dict[str, Any], entry)
        routes.append(RuntimeRoute(method=str(route.get("method", "")), path=str(route.get("path", ""))))
    if len({(route.method, route.path) for route in routes}) != len(routes):
        raise RuntimeRoleError("Generated runtime route metadata contains duplicates")
    return tuple(routes)


@dataclass(frozen=True)
class RuntimeRoleDeployment:
    """Deployment-owned values the runtime role policy is scoped to."""

    partition: str
    account_id: str
    region: str
    ingestion_bucket_name: str
    destination_key_arn: str
    permissions_boundary_arn: str
    runtime_routes: tuple[RuntimeRoute, ...]

    def __post_init__(self) -> None:
        missing = [
            name
            for name, value in (
                ("partition", self.partition),
                ("account_id", self.account_id),
                ("region", self.region),
                ("ingestion_bucket_name", self.ingestion_bucket_name),
                ("destination_key_arn", self.destination_key_arn),
                ("permissions_boundary_arn", self.permissions_boundary_arn),
                ("runtime_routes", self.runtime_routes),
            )
            if not value
        ]
        if missing:
            raise RuntimeRoleError("Runtime role deployment configuration is incomplete")


@dataclass(frozen=True)
class RuntimeRoleIdentity:
    """Tenant and connector identity owning one runtime role."""

    tenant: TenantContext
    connector_id: str


@dataclass(frozen=True)
class RuntimeRolePolicyRequest:
    """Runtime role identity, serving REST API, and exact assumable source role."""

    identity: RuntimeRoleIdentity
    source_role_arn: str
    rest_api_id: str

    def __post_init__(self) -> None:
        if _REST_API_ID.fullmatch(self.rest_api_id) is None:
            raise RuntimeRoleError("Connector runtime role requires the serving REST API id")


def create_object_prefix(tenant: TenantContext, connector_id: str) -> str:
    """Return the service-authored destination prefix for one connector."""
    return f"documents/{tenant.account_id}/{connector_id}/"


class ConnectorRuntimeRoleLifecycle:
    """Creates, re-scopes, and deletes one runtime role per connector."""

    def __init__(
        self,
        iam_client: RuntimeRoleIamClient,
        deployment: RuntimeRoleDeployment,
    ) -> None:
        self._iam_client = iam_client
        self._deployment = deployment

    def create(self, request: RuntimeRolePolicyRequest) -> str:
        """Create the runtime role and its single inline policy idempotently."""
        role_name = self.role_name(request.identity)
        created_now = True
        try:
            self._iam_client.create_role(
                Path=RUNTIME_ROLE_PATH,
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(self._trust_policy(), separators=(",", ":")),
                Description="CCF per-connector runtime role.",
                MaxSessionDuration=_RUNTIME_ROLE_MAX_SESSION_SECONDS,
                PermissionsBoundary=self._deployment.permissions_boundary_arn,
            )
        except ClientError as error:
            if _error_code(error) not in _ALREADY_EXISTS_CODES:
                raise RuntimeRoleError("Connector runtime role creation failed") from error
            created_now = False
        try:
            self.replace_policy(request)
        except Exception:
            # A role created in this call but left without its scoping policy
            # would be an unusable identity under the permissions boundary.
            # A pre-existing role still carries its previous policy, so a
            # replacement failure must leave that role untouched.
            if created_now:
                self.delete(request.identity)
            raise
        return self.role_arn(role_name)

    def replace_policy(self, request: RuntimeRolePolicyRequest) -> None:
        """Replace the inline policy so it names only current connector resources."""
        try:
            self._iam_client.put_role_policy(
                RoleName=self.role_name(request.identity),
                PolicyName=RUNTIME_ROLE_POLICY_NAME,
                PolicyDocument=json.dumps(self._access_policy(request), separators=(",", ":")),
            )
        except ClientError as error:
            raise RuntimeRoleError("Connector runtime role policy update failed") from error

    def delete(self, identity: RuntimeRoleIdentity) -> None:
        """Delete the inline policy and the role, treating absence as success."""
        role_name = self.role_name(identity)
        self._delete_inline_policy(role_name)
        try:
            self._iam_client.delete_role(RoleName=role_name)
        except ClientError as error:
            if _error_code(error) not in _MISSING_CODES:
                raise RuntimeRoleError("Connector runtime role deletion failed") from error

    def role_arn(self, role_name: str) -> str:
        """Return the exact ARN of one runtime role."""
        deployment = self._deployment
        return f"arn:{deployment.partition}:iam::{deployment.account_id}:role{RUNTIME_ROLE_PATH}{role_name}"

    @staticmethod
    def role_name(identity: RuntimeRoleIdentity) -> str:
        """Return the deterministic role name for one tenant connector."""
        resource_identity = f"{identity.tenant.get_arn_prefix()}:{identity.connector_id}"
        identity_hash = hashlib.sha256(resource_identity.encode("utf-8")).hexdigest()
        return f"{RUNTIME_ROLE_NAME_PREFIX}{identity_hash[:_IDENTITY_HASH_LENGTH]}"

    def _delete_inline_policy(self, role_name: str) -> None:
        try:
            self._iam_client.delete_role_policy(
                RoleName=role_name,
                PolicyName=RUNTIME_ROLE_POLICY_NAME,
            )
        except ClientError as error:
            if _error_code(error) not in _MISSING_CODES:
                raise RuntimeRoleError("Connector runtime role policy deletion failed") from error

    def _trust_policy(self) -> dict[str, Any]:
        deployment = self._deployment
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": deployment.account_id},
                        "ArnLike": {
                            "aws:SourceArn": (
                                f"arn:{deployment.partition}:ecs:{deployment.region}:{deployment.account_id}:*"
                            )
                        },
                    },
                }
            ],
        }

    def _access_policy(self, request: RuntimeRolePolicyRequest) -> dict[str, Any]:
        deployment = self._deployment
        connector_id = request.identity.connector_id
        object_prefix = create_object_prefix(request.identity.tenant, connector_id)
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ConnectorApiResources",
                    "Effect": "Allow",
                    "Action": "execute-api:Invoke",
                    "Resource": list(self._api_resources(connector_id, request.rest_api_id)),
                },
                {
                    "Sid": "ConnectorDestinationPrefix",
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:DeleteObject"],
                    "Resource": [
                        f"arn:{deployment.partition}:s3:::{deployment.ingestion_bucket_name}/{object_prefix}*"
                    ],
                },
                {
                    "Sid": "ConnectorDestinationEncryption",
                    "Effect": "Allow",
                    "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
                    "Resource": [deployment.destination_key_arn],
                    "Condition": {"StringEquals": {"kms:ViaService": f"s3.{deployment.region}.amazonaws.com"}},
                },
                {
                    "Sid": "ConnectorSourceRole",
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Resource": [request.source_role_arn],
                },
            ],
        }

    def _api_resources(self, connector_id: str, rest_api_id: str) -> Sequence[str]:
        """Build exact per-connector ARNs from the generated route templates."""
        deployment = self._deployment
        return [
            f"arn:{deployment.partition}:execute-api:{deployment.region}:"
            f"{deployment.account_id}:{rest_api_id}/*/{route.for_connector(connector_id)}"
            for route in deployment.runtime_routes
        ]


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))
