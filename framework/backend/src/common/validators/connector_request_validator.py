"""The custom connector checks that neither API Gateway's validator nor AWS runs.

API Gateway has already matched every member against its Smithy pattern, so each value here is
well formed. What the patterns can't say is that the account, Region, partition, and DNS suffix
are the caller's and the service's, that Fargate runs the task size, and that the configuration
fits in 65,536 UTF-8 bytes. Each check splits the already-valid value at `.` or `:` and compares
the parts.
"""

from __future__ import annotations

from ccf_api_models.base import ValueModel
from ccf_api_models.identifiers import TenantContext
from ccf_api_models.models import CreateCustomConnectorRequest, ResourceRequirements, UpdateCustomConnectorRequest

from common.errors import BadRequestError
from common.operation import OperationRequest
from common.validators.byte_limits import FieldOfModel, Utf8ByteLimit

CONNECTOR_CONFIGURATION_LIMIT = Utf8ByteLimit.of(
    FieldOfModel(model=CreateCustomConnectorRequest, field_name="connector_configuration")
)

_GIB = 1024
_FARGATE_MEMORY_MIB_BY_CPU: dict[float, frozenset[int]] = {
    0.5: frozenset(range(1 * _GIB, 4 * _GIB + 1, _GIB)),
    1.0: frozenset(range(2 * _GIB, 8 * _GIB + 1, _GIB)),
    2.0: frozenset(range(4 * _GIB, 16 * _GIB + 1, _GIB)),
    4.0: frozenset(range(8 * _GIB, 30 * _GIB + 1, _GIB)),
    8.0: frozenset(range(16 * _GIB, 60 * _GIB + 1, 4 * _GIB)),
    16.0: frozenset(range(32 * _GIB, 120 * _GIB + 1, 8 * _GIB)),
}
"""The memory values, in MiB, Fargate accepts with each vCPU value (AWS Batch API reference, `ResourceRequirement`)."""

type ConnectorWriteRequest = (
    OperationRequest[CreateCustomConnectorRequest] | OperationRequest[UpdateCustomConnectorRequest]
)


class _SentValue(ValueModel):
    """One member the request sends, by its dotted name, and the caller it must belong to."""

    tenant: TenantContext
    member: str
    value: str


class ConnectorRequestValidator:
    """Raises BadRequestError naming the first member that isn't the caller's or that AWS can't run."""

    def __init__(self, region: str, url_suffix: str) -> None:
        self._region = region
        self._url_suffix = url_suffix

    def validate(self, request: ConnectorWriteRequest) -> None:
        """Check, in order: the image, the task size, both tenant roles, the secret and log group, the configuration."""
        tenant = request.tenant
        payload = request.payload
        container = payload.container_properties
        if container is not None and container.image_uri is not None:
            self._check_image(
                _SentValue(tenant=tenant, member="container_properties.image_uri", value=container.image_uri)
            )
        if container is not None and container.resource_requirements is not None:
            self._check_task_size(container.resource_requirements)
        source_access = payload.source_access
        log_destination = payload.log_destination
        if source_access is not None:
            self._check_role(_SentValue(tenant=tenant, member="source_access.role_arn", value=source_access.role_arn))
        if log_destination is not None:
            self._check_role(
                _SentValue(tenant=tenant, member="log_destination.role_arn", value=log_destination.role_arn)
            )
        if source_access is not None and source_access.secret_arn is not None:
            self._check_regional_arn(
                _SentValue(tenant=tenant, member="source_access.secret_arn", value=source_access.secret_arn)
            )
        if log_destination is not None:
            self._check_regional_arn(_SentValue(tenant=tenant, member="log_destination.arn", value=log_destination.arn))
        if payload.connector_configuration is not None:
            CONNECTOR_CONFIGURATION_LIMIT.check(payload.connector_configuration)

    def _check_image(self, sent: _SentValue) -> None:
        """`<account>.dkr.ecr.<region>.<suffix>/...`: the caller's account, the service's Region and suffix."""
        host = sent.value.split("/", 1)[0]
        account_id, _, _, region, url_suffix = host.split(".", 4)
        if (account_id, region, url_suffix) != (sent.tenant.account_id, self._region, self._url_suffix):
            raise BadRequestError(
                "The image isn't in the caller's account and the service Region", fields=(sent.member,)
            )

    @staticmethod
    def _check_role(sent: _SentValue) -> None:
        """`arn:<partition>:iam::<account>:role/<name>`: the caller's partition and account."""
        _, partition, _, _, account_id, _ = sent.value.split(":", 5)
        if (partition, account_id) != (sent.tenant.partition.value, sent.tenant.account_id):
            raise BadRequestError("The role isn't in the caller's account", fields=(sent.member,))

    def _check_regional_arn(self, sent: _SentValue) -> None:
        """`arn:<partition>:<service>:<region>:<account>:...`: the caller's partition and account, this Region."""
        _, partition, _, region, account_id, _ = sent.value.split(":", 5)
        expected = (sent.tenant.partition.value, self._region, sent.tenant.account_id)
        if (partition, region, account_id) != expected:
            raise BadRequestError(
                "The resource isn't in the caller's account and the service Region", fields=(sent.member,)
            )

    @staticmethod
    def _check_task_size(requirements: ResourceRequirements) -> None:
        if requirements.memory not in _FARGATE_MEMORY_MIB_BY_CPU.get(requirements.cpu, frozenset()):
            raise BadRequestError(
                "Fargate doesn't run this task size", fields=("container_properties.resource_requirements",)
            )
