"""Shared behavior for generated CCF Pydantic models."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, SecretBytes, SecretStr


def _reveal_for_wire(value: Any) -> Any:
    """Reveal nested Pydantic secret values only for explicit wire transport."""
    if isinstance(value, (SecretStr, SecretBytes)):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {key: _reveal_for_wire(nested_value) for key, nested_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_reveal_for_wire(nested_value) for nested_value in value]
    return value


class CcfModel(BaseModel):
    """Base for strict generated models with opt-in transport serialization."""

    model_config = ConfigDict(extra="forbid", strict=True)

    def model_dump_for_wire(self) -> dict[str, Any]:
        """Return explicitly set JSON-ready values for authorized transport."""
        return cast(dict[str, Any], _reveal_for_wire(self.model_dump(exclude_unset=True)))
