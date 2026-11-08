"""External IDs: the value each source-access and log-delivery role's trust policy requires.

The framework generates one per custom connector, so no tenant can trick it into assuming
another tenant's role (the confused deputy problem). It is a guard, not a secret.
"""

from __future__ import annotations

import secrets

EXTERNAL_ID_PREFIX = "ccf-ext-"
_RANDOM_BYTES = 16


class ExternalIdGenerator:
    """Generates `ccf-ext-` plus 32 random hex characters."""

    def generate(self) -> str:
        return f"{EXTERNAL_ID_PREFIX}{secrets.token_hex(_RANDOM_BYTES)}"
