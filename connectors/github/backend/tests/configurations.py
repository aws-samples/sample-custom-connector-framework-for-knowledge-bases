"""The connector configuration the unit and live tests start from."""

import json

from common.configuration import GitHubConnectorConfiguration

OWNER = "aws-samples"


def make_configuration() -> GitHubConnectorConfiguration:
    """Two repositories, small files, no pacing, no retries."""
    return GitHubConnectorConfiguration.model_validate_json(
        json.dumps(
            {
                "owner": OWNER,
                "repositories": [],
                "max_repositories": 2,
                "max_repository_age_days": 730,
                "excluded_repository_patterns": ["aws-sdk*", "*-sdk"],
                "excluded_path_patterns": ["node_modules/", "__pycache__/", "*.py[cod]", "dist/", ".env", "*.so"],
                "max_file_bytes": 1024,
                "full_read_interval_days": 28,
                "github": {
                    "request_interval_seconds": 0.0,
                    "request_timeout_seconds": 5,
                    "retry_attempts": 0,
                    "retry_backoff_seconds": 0.0,
                    "max_rate_limit_wait_seconds": 1.0,
                },
            }
        )
    )
