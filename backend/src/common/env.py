"""Centralized runtime configuration for backend handlers."""

import os

_ARN_PART_COUNT = 6
_AWS_ACCOUNT_ID_LENGTH = 12


def _configuration_key_identity(configuration_key_arn: str) -> tuple[str, str]:
    """Return deployment partition and account only for a valid KMS key ARN."""
    arn_parts = configuration_key_arn.split(":", _ARN_PART_COUNT - 1)
    if len(arn_parts) != _ARN_PART_COUNT or arn_parts[0] != "arn" or arn_parts[2] != "kms":
        return "", ""
    partition, account_id = arn_parts[1], arn_parts[4]
    if not partition or len(account_id) != _AWS_ACCOUNT_ID_LENGTH or not account_id.isdigit():
        return "", ""
    return partition, account_id


CONFIGURATION_KEY_ARN = os.environ.get("CONFIGURATION_KEY_ARN", "")
_configuration_partition, _configuration_account_id = _configuration_key_identity(CONFIGURATION_KEY_ARN)

API_PREFIX = os.environ.get("API_PREFIX", "/api/v1")
CUSTOM_CONNECTORS_TABLE_NAME = os.environ.get("CUSTOM_CONNECTORS_TABLE_NAME", "CustomConnectors")
CUSTOM_CONNECTOR_JOBS_TABLE_NAME = os.environ.get("CUSTOM_CONNECTOR_JOBS_TABLE_NAME", "CustomConnectorJobs")
CUSTOM_CONNECTOR_DOCUMENTS_TABLE_NAME = os.environ.get(
    "CUSTOM_CONNECTOR_DOCUMENTS_TABLE_NAME", "CustomConnectorDocuments"
)
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
AWS_PARTITION = os.environ.get("AWS_PARTITION", _configuration_partition)
FRAMEWORK_ACCOUNT_ID = os.environ.get("FRAMEWORK_ACCOUNT_ID", _configuration_account_id)
CCF_SERVICE_PRINCIPAL = os.environ.get("CCF_SERVICE_PRINCIPAL", "ccf.amazonaws.com")
AWS_BATCH_JOB_QUEUE = os.environ.get("AWS_BATCH_JOB_QUEUE")
CUSTOM_CONNECTOR_API_ENDPOINT = os.environ.get("CUSTOM_CONNECTOR_API_ENDPOINT")
INGESTION_BUCKET_NAME = os.environ.get("INGESTION_BUCKET_NAME", "")
DESTINATION_MODE = os.environ.get("DESTINATION_MODE", "S3")
FRAMEWORK_EXECUTION_ROLE_ARN = os.environ.get("FRAMEWORK_EXECUTION_ROLE_ARN", "")
RUNTIME_ROLE_BOUNDARY_ARN = os.environ.get("RUNTIME_ROLE_BOUNDARY_ARN", "")
CONNECTOR_RUNTIME_ROUTES = os.environ.get("CONNECTOR_RUNTIME_ROUTES", "")
ALLOWED_CLIENT_ACCOUNT_IDS = os.environ.get("ALLOWED_CLIENT_ACCOUNT_IDS", "")
SCHEDULER_TARGET_ARN = os.environ.get("SCHEDULER_TARGET_ARN", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
SCHEDULER_GROUP_NAME = os.environ.get("SCHEDULER_GROUP_NAME", "default")
SCHEDULER_DLQ_ARN = os.environ.get("SCHEDULER_DLQ_ARN", "")
BEDROCK_KNOWLEDGE_BASE_ID = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID", "")
BEDROCK_DATA_SOURCE_ID = os.environ.get("BEDROCK_DATA_SOURCE_ID", "")
