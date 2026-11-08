"""Job inputs and a runner wired to the testing module's fakes."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ccf_api_models.base import ValueModel
from ccf_api_models.job_container import JobExitCode
from ccf_api_models.models import (
    Connector,
    ConnectorStatus,
    ContainerProperties,
    Destination,
    DestinationResources,
    DestinationStatus,
    DestinationType,
    Job,
    JobStatus,
    ResourceRequirements,
)
from ccf_api_models.models import (
    SourceAccess as SourceAccessModel,
)
from pydantic import SecretStr

from custom_connector_framework.client.pages import Pages
from custom_connector_framework.client.send_deadline import SendDeadline
from custom_connector_framework.implementation import ConnectorImplementation
from custom_connector_framework.runner.file_sweeper import FileSweeper
from custom_connector_framework.runner.job_inputs_reader import JobIdentity, JobInputs, JobInputsReader
from custom_connector_framework.runner.job_runner import JobRunner
from custom_connector_framework.runner.secret_reader import SecretReader
from custom_connector_framework.runner.source_access import SourceAccess
from custom_connector_framework.runner.termination_signal import TerminationSignal
from custom_connector_framework.testing import FakeCcfApi, FakeS3, FakeSecretsManager, FakeSessions, FakeSts

if TYPE_CHECKING:
    from mypy_boto3_sts.client import STSClient

TENANT = "111122223333"
SERVICE_ACCOUNT = "444455556666"
REGION = "us-east-1"
TENANT_ARN_PREFIX = f"arn:aws:ccf:{REGION}:{TENANT}"
DESTINATION_ID = "dst-7c41e9a2b5d0"
CONNECTOR_ID = "cc-e020d65d76c0"
JOB_ID = "ccj-5b8e0d2c41f9"
BUCKET = f"ccf-dst-7c41e9a2b5d0-{TENANT}-{REGION}"
SOURCE_ROLE_ARN = f"arn:aws:iam::{TENANT}:role/CcfSourceRole-github-awslabs"
EXTERNAL_ID = "ccf-ext-2f9d41c7a8b04e1f9c3d5a6b7e8f9012"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{TENANT}:secret:github-token-AbCdEf"
SECRET_VALUE = "ghp_" + "1" * 36
DESTINATION_ARN = f"{TENANT_ARN_PREFIX}:destination/{DESTINATION_ID}"
IDENTITY = JobIdentity(
    tenant_arn_prefix=TENANT_ARN_PREFIX,
    region=REGION,
    destination_id=DESTINATION_ID,
    connector_id=CONNECTOR_ID,
    job_id=JOB_ID,
)


class InputsRequest(ValueModel):
    """What to vary in the job inputs."""

    secret_arn: str | None = SECRET_ARN
    connector_configuration: str | None = '{"owner": "awslabs"}'
    job_configuration: str | None = None
    destination_type: DestinationType = DestinationType.BEDROCK
    access_control_enabled: bool = False
    bucket_name: str | None = BUCKET


def job_inputs(request: InputsRequest) -> JobInputs:
    return JobInputs(
        job=Job(
            job_id=JOB_ID,
            arn=f"{DESTINATION_ARN}/custom-connector/{CONNECTOR_ID}/job/{JOB_ID}",
            status=JobStatus.RUNNING,
            created_at=1.0,
            updated_at=1.0,
            job_configuration=None if request.job_configuration is None else SecretStr(request.job_configuration),
        ),
        connector=Connector(
            connector_id=CONNECTOR_ID,
            arn=f"{DESTINATION_ARN}/custom-connector/{CONNECTOR_ID}",
            destination_id=DESTINATION_ID,
            name="github-awslabs",
            status=ConnectorStatus.IN_USE,
            created_at=1.0,
            updated_at=1.0,
            runtime_role_arn=f"arn:aws:iam::{SERVICE_ACCOUNT}:role/ccf-runtime/CcfConnectorRuntime-3c9e1f0a2b7d4e6f8a1b2c3d4e5f6a7b",
            container_properties=ContainerProperties(
                image_uri=f"{TENANT}.dkr.ecr.{REGION}.amazonaws.com/ccf-connectors/github:abc",
                command=["ccf-connector", "run", "connector:GitHub"],
                resource_requirements=ResourceRequirements(cpu=1.0, memory=2048),
                timeout=3600,
            ),
            connector_configuration=(
                None if request.connector_configuration is None else SecretStr(request.connector_configuration)
            ),
            source_access=SourceAccessModel(
                role_arn=SOURCE_ROLE_ARN, secret_arn=request.secret_arn, external_id=EXTERNAL_ID
            ),
        ),
        destination=Destination(
            destination_id=DESTINATION_ID,
            arn=DESTINATION_ARN,
            name="github",
            type=request.destination_type,
            access_control_enabled=request.access_control_enabled,
            status=DestinationStatus.ACTIVE,
            role_arn=f"arn:aws:iam::{TENANT}:role/ccf/CcfDestinationRole",
            destination_resources=DestinationResources(
                bucket_arn=None if request.bucket_name is None else f"arn:aws:s3:::{request.bucket_name}",
                bucket_name=request.bucket_name,
            ),
            created_at=1.0,
            updated_at=1.0,
        ),
    )


class RunnerWorld:
    """A runner and the fakes behind it; the send deadline's clock stands still."""

    def __init__(self, inputs: InputsRequest | None = None) -> None:
        self.api = FakeCcfApi()
        self.api.job_inputs = job_inputs(inputs or InputsRequest())
        self.sts = FakeSts()
        self.secrets = FakeSecretsManager(secrets={SECRET_ARN: SECRET_VALUE})
        self.s3 = FakeS3()
        self.sessions = FakeSessions({"s3": self.s3, "secretsmanager": self.secrets})
        self.send_deadline = SendDeadline(lambda: 0.0)

    def runner(self) -> JobRunner:
        pages = Pages(self.api)
        return JobRunner(
            IDENTITY,
            inputs=JobInputsReader(self.api),
            source_access=SourceAccess(cast("STSClient", self.sts), self.sessions),
            secrets=SecretReader(),
            api=self.api,
            pages=pages,
            sweeper=FileSweeper(pages),
            termination=TerminationSignal(),
            send_deadline=self.send_deadline,
        )

    def run(self, implementation: type[ConnectorImplementation]) -> JobExitCode:
        return self.runner().run(implementation).exit_code
