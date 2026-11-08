"""Every generated model is a strict, closed `CcfModel`, and its secret members show only in the wire dump."""

import json

import ccf_api_models.models as models
from ccf_api_models.base import WIRE_SECRET_MEMBERS, CcfModel
from pydantic import BaseModel, SecretStr

CHECKPOINT_VALUE = "checkpoint-value"
GENERATED = [
    member
    for member in vars(models).values()
    if isinstance(member, type)
    and issubclass(member, BaseModel)
    and member.__module__ == models.__name__
    and not getattr(member, "__pydantic_root_model__", False)
]


def test_every_generated_model_is_a_strict_closed_ccf_model() -> None:
    assert GENERATED
    assert all(
        issubclass(model, CcfModel) and model.model_config.get("extra") == "forbid" and model.model_config.get("strict")
        for model in GENERATED
    )


def test_wire_secret_members_names_every_secret_member() -> None:
    secret_members = {
        name
        for model in GENERATED
        for name, field in model.model_fields.items()
        if field.annotation in (SecretStr, SecretStr | None)
    }

    assert secret_members == set(WIRE_SECRET_MEMBERS)


def _checkpoint_request() -> models.PutCustomConnectorCheckpointRequest:
    return models.PutCustomConnectorCheckpointRequest.model_validate_json(
        json.dumps(
            {
                "destination_id": "dst-7c41e9a2b5d0",
                "connector_id": "cc-e020d65d76c0",
                "checkpoint_data": CHECKPOINT_VALUE,
            }
        )
    )


def test_a_secret_is_masked_in_repr_and_json() -> None:
    request = _checkpoint_request()

    assert CHECKPOINT_VALUE not in repr(request)
    assert CHECKPOINT_VALUE not in request.model_dump_json()


def test_the_wire_dump_reveals_a_secret() -> None:
    assert _checkpoint_request().model_dump_for_wire()["checkpoint_data"] == CHECKPOINT_VALUE
