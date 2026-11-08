"""The UTF-8 byte-size check for opaque string members, which CCF stores without parsing.

The model's `@length` counts characters, and API Gateway enforces it; a member within its
character limit can still be too large in bytes. The byte limit is the same number as the
character limit, read from the generated field. Values are never parsed.
"""

from __future__ import annotations

from annotated_types import MaxLen
from ccf_api_models.base import CcfModel, ValueModel
from pydantic import SecretStr

from common.errors import BadRequestError


class FieldOfModel(ValueModel):
    model: type[CcfModel]
    field_name: str


class Utf8ByteLimit(ValueModel):
    field_name: str
    maximum_bytes: int

    @classmethod
    def of(cls, field: FieldOfModel) -> Utf8ByteLimit:
        """The limit whose maximum is the generated field's `max_length`."""
        [maximum] = [
            item.max_length for item in field.model.model_fields[field.field_name].metadata if isinstance(item, MaxLen)
        ]
        return cls(field_name=field.field_name, maximum_bytes=maximum)

    def check(self, value: SecretStr) -> None:
        if len(value.get_secret_value().encode("utf-8")) > self.maximum_bytes:
            raise BadRequestError("A member is over its byte limit", fields=(self.field_name,))
