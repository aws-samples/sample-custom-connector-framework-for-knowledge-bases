"""Base classes for the generated CCF models and the hand-written values beside them."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr, SerializationInfo, field_serializer

WIRE_SECRET_MEMBERS = ("checkpoint_data", "connector_configuration", "job_configuration")
"""The `SecretStr` members of the generated models; `tests/test_ccf_model.py` checks that the list is complete."""

_WIRE_CONTEXT = object()
"""The serialization context of `model_dump_for_wire`, the one dump that reveals secret members."""


class CcfModel(BaseModel):
    """Base for strict generated models with opt-in transport serialization."""

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_serializer(*WIRE_SECRET_MEMBERS, check_fields=False)
    def _reveal_for_wire(self, secret: SecretStr | None, info: SerializationInfo) -> SecretStr | str | None:
        """Return the secret's value in the wire dump and the masked secret in every other dump."""
        if secret is None or info.context is not _WIRE_CONTEXT:
            return secret
        return secret.get_secret_value()

    def model_dump_for_wire(self) -> dict[str, Any]:
        """Return explicitly set JSON-ready values for authorized transport."""
        return self.model_dump(exclude_unset=True, context=_WIRE_CONTEXT)


class ValueModel(BaseModel):
    """Base for immutable values that never cross the wire: references, keys, settings, and requests."""

    model_config = ConfigDict(frozen=True, extra="forbid")
