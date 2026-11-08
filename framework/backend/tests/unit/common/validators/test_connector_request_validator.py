"""A connector create or update must name the caller's resources, a Fargate task size, and a small configuration."""

from __future__ import annotations

from typing import Any

import pytest
from ccf_api_models.identifiers import Partition, TenantContext
from ccf_api_models.models import (
    ContainerProperties,
    CreateCustomConnectorRequest,
    LogDestination,
    LogDestinationType,
    ResourceRequirements,
    SourceAccessInput,
    UpdateContainerProperties,
    UpdateCustomConnectorRequest,
)
from pydantic import SecretStr

from common.errors import BadRequestError
from common.operation import OperationRequest, RequestTrace
from common.validators.connector_request_validator import ConnectorRequestValidator

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")
VALIDATOR = ConnectorRequestValidator("us-east-1", "amazonaws.com")
TRACE = RequestTrace(correlation_id="3f6c2d1e-8b4a-4c7e-9f10-2a5b6c7d8e9f", api_request_id="c8f9c4b5")
IMAGE_URI = "111122223333.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:abc"
SOURCE_ROLE_ARN = "arn:aws:iam::111122223333:role/CcfSourceRole-github"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:github-token"
LOG_GROUP_ARN = "arn:aws:logs:us-east-1:111122223333:log-group:/ccf/connectors/github"
LOG_ROLE_ARN = "arn:aws:iam::111122223333:role/CcfLogDeliveryRole-github"


def create(**overrides: Any) -> OperationRequest[CreateCustomConnectorRequest]:
    values: dict[str, Any] = {
        "destination_id": "dst-7c41e9a2b5d0",
        "name": "github",
        "container_properties": container(),
        "source_access": SourceAccessInput(role_arn=SOURCE_ROLE_ARN, secret_arn=SECRET_ARN),
        "log_destination": log_destination(),
        **overrides,
    }
    return OperationRequest(tenant=TENANT, payload=CreateCustomConnectorRequest(**values), trace=TRACE)


def update(**members: Any) -> OperationRequest[UpdateCustomConnectorRequest]:
    payload = UpdateCustomConnectorRequest(destination_id="dst-7c41e9a2b5d0", connector_id="cc-0a1b2c3d4e5f", **members)
    return OperationRequest(tenant=TENANT, payload=payload, trace=TRACE)


def container(image_uri: str = IMAGE_URI, cpu: float = 1.0, memory: int = 2048) -> ContainerProperties:
    return ContainerProperties(
        image_uri=image_uri,
        command=["ccf-connector"],
        resource_requirements=ResourceRequirements(cpu=cpu, memory=memory),
        timeout=3600,
    )


def log_destination(arn: str = LOG_GROUP_ARN, role_arn: str = LOG_ROLE_ARN) -> LogDestination:
    return LogDestination(type=LogDestinationType.CLOUDWATCH_LOGS, arn=arn, role_arn=role_arn)


def rejected_member(request: OperationRequest[Any]) -> tuple[str, ...]:
    with pytest.raises(BadRequestError) as raised:
        VALIDATOR.validate(request)
    return raised.value.fields


def test_a_valid_create_passes() -> None:
    VALIDATOR.validate(create(connector_configuration=SecretStr('{"owner": "awslabs"}')))


def test_a_valid_update_passes() -> None:
    VALIDATOR.validate(
        update(
            container_properties=UpdateContainerProperties(image_uri=IMAGE_URI),
            source_access=SourceAccessInput(role_arn=SOURCE_ROLE_ARN),
            log_destination=log_destination(arn=f"{LOG_GROUP_ARN}:*"),
        )
    )


@pytest.mark.parametrize(
    "image_uri",
    [
        pytest.param("444455556666.dkr.ecr.us-east-1.amazonaws.com/ccf-connectors/github:abc", id="another-account"),
        pytest.param("111122223333.dkr.ecr.us-west-2.amazonaws.com/ccf-connectors/github:abc", id="another-region"),
        pytest.param(
            "111122223333.dkr.ecr.cn-north-1.amazonaws.com.cn/ccf-connectors/github:abc", id="another-dns-suffix"
        ),
    ],
)
def test_an_image_outside_the_callers_account_or_the_service_region_is_rejected(image_uri: str) -> None:
    assert rejected_member(create(container_properties=container(image_uri=image_uri))) == (
        "container_properties.image_uri",
    )


def test_an_update_that_sends_an_image_in_another_account_is_rejected() -> None:
    image_uri = IMAGE_URI.replace("111122223333", "444455556666")

    assert rejected_member(update(container_properties=UpdateContainerProperties(image_uri=image_uri))) == (
        "container_properties.image_uri",
    )


def test_an_image_in_the_aws_eusc_partition_passes_in_an_aws_eusc_service() -> None:
    validator = ConnectorRequestValidator("eusc-de-east-1", "amazonaws.eu")
    tenant = TenantContext(partition=Partition.AWS_EUSC, region="eusc-de-east-1", account_id="111122223333")
    image_uri = "111122223333.dkr.ecr.eusc-de-east-1.amazonaws.eu/ccf-connectors/github:1.0.0"
    payload = UpdateCustomConnectorRequest(
        destination_id="dst-7c41e9a2b5d0",
        connector_id="cc-0a1b2c3d4e5f",
        container_properties=UpdateContainerProperties(image_uri=image_uri),
    )

    validator.validate(OperationRequest(tenant=tenant, payload=payload, trace=TRACE))


@pytest.mark.parametrize(
    ("source_access", "member"),
    [
        pytest.param(
            SourceAccessInput(role_arn="arn:aws:iam::444455556666:role/CcfSourceRole-github"),
            "source_access.role_arn",
            id="role-in-another-account",
        ),
        pytest.param(
            SourceAccessInput(role_arn="arn:aws-cn:iam::111122223333:role/CcfSourceRole-github"),
            "source_access.role_arn",
            id="role-in-another-partition",
        ),
        pytest.param(
            SourceAccessInput(role_arn=SOURCE_ROLE_ARN, secret_arn=SECRET_ARN.replace("111122223333", "444455556666")),
            "source_access.secret_arn",
            id="secret-in-another-account",
        ),
        pytest.param(
            SourceAccessInput(role_arn=SOURCE_ROLE_ARN, secret_arn=SECRET_ARN.replace("us-east-1", "us-west-2")),
            "source_access.secret_arn",
            id="secret-in-another-region",
        ),
        pytest.param(
            SourceAccessInput(role_arn=SOURCE_ROLE_ARN, secret_arn=SECRET_ARN.replace("arn:aws:", "arn:aws-us-gov:")),
            "source_access.secret_arn",
            id="secret-in-another-partition",
        ),
    ],
)
def test_a_source_role_or_secret_that_isnt_the_callers_is_rejected(
    source_access: SourceAccessInput, member: str
) -> None:
    assert rejected_member(create(source_access=source_access)) == (member,)


@pytest.mark.parametrize(
    ("destination", "member"),
    [
        pytest.param(
            log_destination(role_arn=LOG_ROLE_ARN.replace("111122223333", "444455556666")),
            "log_destination.role_arn",
            id="role-in-another-account",
        ),
        pytest.param(
            log_destination(arn=LOG_GROUP_ARN.replace("111122223333", "444455556666")),
            "log_destination.arn",
            id="log-group-in-another-account",
        ),
        pytest.param(
            log_destination(arn=LOG_GROUP_ARN.replace("us-east-1", "us-west-2")),
            "log_destination.arn",
            id="log-group-in-another-region",
        ),
        pytest.param(
            log_destination(arn=LOG_GROUP_ARN.replace("arn:aws:", "arn:aws-cn:")),
            "log_destination.arn",
            id="log-group-in-another-partition",
        ),
    ],
)
def test_a_log_destination_that_isnt_the_callers_is_rejected(destination: LogDestination, member: str) -> None:
    assert rejected_member(update(log_destination=destination)) == (member,)


@pytest.mark.parametrize(("cpu", "memory"), [(0.5, 1024), (0.5, 4096), (1.0, 8192), (2.0, 16384), (16.0, 122880)])
def test_a_task_size_fargate_runs_passes(cpu: float, memory: int) -> None:
    VALIDATOR.validate(create(container_properties=container(cpu=cpu, memory=memory)))


@pytest.mark.parametrize(("cpu", "memory"), [(0.5, 8192), (1.0, 1024), (0.75, 2048), (8.0, 18432), (2.0, 2048)])
def test_a_task_size_fargate_doesnt_run_is_rejected(cpu: float, memory: int) -> None:
    assert rejected_member(create(container_properties=container(cpu=cpu, memory=memory))) == (
        "container_properties.resource_requirements",
    )


def test_an_update_that_sends_a_task_size_fargate_doesnt_run_is_rejected() -> None:
    requirements = ResourceRequirements(cpu=0.5, memory=8192)

    assert rejected_member(
        update(container_properties=UpdateContainerProperties(resource_requirements=requirements))
    ) == ("container_properties.resource_requirements",)


def test_a_configuration_one_utf8_byte_over_the_limit_is_rejected() -> None:
    too_large = "€" * 21_845 + "aa"
    assert len(too_large.encode()) == 65_537

    assert rejected_member(update(connector_configuration=SecretStr(too_large))) == ("connector_configuration",)


def test_a_configuration_at_the_limit_passes() -> None:
    VALIDATOR.validate(update(connector_configuration=SecretStr("€" * 21_845 + "a")))
