"""The settings a `connector_ops` handler module reads at import; the tests build their own handlers over fakes."""

import os

os.environ.setdefault("FRAMEWORK_ENDPOINT", "https://abcdef1234.execute-api.us-east-1.amazonaws.com/ccf")
os.environ.setdefault("SERVICE_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
