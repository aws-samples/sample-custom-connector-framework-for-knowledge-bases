"""Each byte limit is the generated field's character limit, counted in UTF-8 bytes."""

from __future__ import annotations

import pytest
from ccf_api_models.models import (
    CreateCustomConnectorRequest,
    PutCustomConnectorCheckpointRequest,
    StartCustomConnectorJobRequest,
)
from pydantic import SecretStr

from common.errors import BadRequestError
from common.validators.byte_limits import FieldOfModel, Utf8ByteLimit

FIELDS = [
    FieldOfModel(model=CreateCustomConnectorRequest, field_name="connector_configuration"),
    FieldOfModel(model=StartCustomConnectorJobRequest, field_name="job_configuration"),
    FieldOfModel(model=PutCustomConnectorCheckpointRequest, field_name="checkpoint_data"),
]


@pytest.mark.parametrize("field", FIELDS, ids=lambda field: field.field_name)
def test_a_multi_byte_value_inside_the_character_limit_and_over_the_byte_limit_is_rejected(
    field: FieldOfModel,
) -> None:
    limit = Utf8ByteLimit.of(field)
    value = "é" * (limit.maximum_bytes // 2 + 1)

    with pytest.raises(BadRequestError) as raised:
        limit.check(SecretStr(value))

    assert len(value) <= limit.maximum_bytes
    assert raised.value.fields == (field.field_name,)


@pytest.mark.parametrize("field", FIELDS, ids=lambda field: field.field_name)
def test_a_value_at_the_byte_limit_passes_without_being_parsed(field: FieldOfModel) -> None:
    limit = Utf8ByteLimit.of(field)

    limit.check(SecretStr("x" * limit.maximum_bytes))


def test_the_limit_is_read_from_the_generated_field() -> None:
    assert Utf8ByteLimit.of(FIELDS[2]).maximum_bytes == 16_384
