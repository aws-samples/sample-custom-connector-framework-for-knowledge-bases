"""Test environment for backend modules and composition roots."""

import json
import os

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_PARTITION", "aws")
os.environ.setdefault("FRAMEWORK_ACCOUNT_ID", "123456789012")
os.environ.setdefault("CCF_SERVICE_PRINCIPAL", "ccf.amazonaws.com")
os.environ.setdefault(
    "CONFIGURATION_KEY_ARN",
    "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
)
os.environ.setdefault("CUSTOM_CONNECTOR_API_ENDPOINT", "https://example.com")
os.environ.setdefault("INGESTION_BUCKET_NAME", "ccf-test-ingestion")
os.environ.setdefault(
    "RUNTIME_ROLE_BOUNDARY_ARN",
    "arn:aws:iam::123456789012:policy/CcfConnectorRuntimeBoundary",
)
# Mirror the generated runtime-callable route templates the deployment
# derives from the Smithy contract and injects into the control plane.
_RUNTIME_ROUTE_PATHS = (
    ("GET", ""),
    ("GET", "/jobs/{job_id}"),
    ("GET", "/checkpoint"),
    ("PUT", "/checkpoint"),
    ("DELETE", "/checkpoint"),
    ("GET", "/documents"),
    ("POST", "/documents"),
    ("DELETE", "/documents"),
)
os.environ.setdefault(
    "CONNECTOR_RUNTIME_ROUTES",
    json.dumps(
        [
            {
                "method": method,
                "path": f"/api/v1/custom-connectors/{{connector_id}}{suffix}",
            }
            for method, suffix in _RUNTIME_ROUTE_PATHS
        ],
        separators=(",", ":"),
    ),
)
os.environ.setdefault("ALLOWED_CLIENT_ACCOUNT_IDS", "123456789012")
