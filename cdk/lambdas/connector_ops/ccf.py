"""Build the signed public CCF client used by connector registration."""

import os

from custom_connector_framework.client import CcfClient


def client() -> CcfClient:
    """Return the public CCF client with default Lambda role credentials."""
    return CcfClient(
        endpoint=os.environ["CCF_ENDPOINT"], region=os.environ["AWS_REGION"]
    )
