"""`CustomResourceHandler`: which resource method each CloudFormation event runs, and what it returns."""

import hashlib
from typing import Any

import pytest
from ccf_api_models.models import ErrorCode
from custom_connector_framework.client import ApiFailure, CcfApiError

from custom_resource import (
    CompletionResponse,
    CustomResourceEvent,
    CustomResourceHandler,
    CustomResourceModel,
    CustomResourceProperties,
    CustomResourceResponse,
)

STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/Example/0f1e2d3c"
LOGICAL_ID = "Thing1A2B3C4D"
PHYSICAL_ID = "thing-1"
PROPERTIES = {
    "ServiceToken": "arn:aws:lambda:us-east-1:111122223333:function:provider",
    "Size": "3",
}


class ThingProperties(CustomResourceProperties):
    size: int


class ThingAttributes(CustomResourceModel):
    thing_id: str


def failure(code: ErrorCode) -> CcfApiError:
    return CcfApiError(ApiFailure(status_code=400, code=code, message="m", request_id="r"))


class FakeThing:
    """Records the calls; `error` is what `delete` and `is_gone` raise, and `ready` what `is_ready` reports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, CustomResourceEvent[ThingProperties]]] = []
        self.error: CcfApiError | None = None
        self.ready = False

    def create(self, event: CustomResourceEvent[ThingProperties]) -> CustomResourceResponse[ThingAttributes]:
        self.calls.append(("create", event))
        return CustomResourceResponse(physical_resource_id=PHYSICAL_ID, data=ThingAttributes(thing_id=PHYSICAL_ID))

    def update(self, event: CustomResourceEvent[ThingProperties]) -> CustomResourceResponse[ThingAttributes]:
        self.calls.append(("update", event))
        return CustomResourceResponse(physical_resource_id=event.existing_id())

    def delete(self, event: CustomResourceEvent[ThingProperties]) -> None:
        self.calls.append(("delete", event))
        if self.error is not None:
            raise self.error

    def is_ready(self, event: CustomResourceEvent[ThingProperties]) -> CompletionResponse[ThingAttributes]:
        self.calls.append(("is_ready", event))
        return CompletionResponse(is_complete=self.ready)

    def is_gone(self, event: CustomResourceEvent[ThingProperties]) -> bool:
        self.calls.append(("is_gone", event))
        if self.error is not None:
            raise self.error
        return False


@pytest.fixture
def thing() -> FakeThing:
    return FakeThing()


@pytest.fixture
def handler(
    thing: FakeThing,
) -> CustomResourceHandler[ThingProperties, ThingAttributes]:
    return CustomResourceHandler(thing, ThingProperties)


def event(request_type: str, **members: Any) -> dict[str, Any]:
    return {
        "RequestType": request_type,
        "StackId": STACK_ID,
        "LogicalResourceId": LOGICAL_ID,
        "ResourceProperties": dict(PROPERTIES),
        **members,
    }


def test_a_create_runs_create_and_returns_its_physical_id_and_data(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes], thing: FakeThing
) -> None:
    response = handler.on_event(event("Create"), None)

    assert response == {
        "PhysicalResourceId": PHYSICAL_ID,
        "Data": {"ThingId": PHYSICAL_ID},
    }
    assert [name for name, _ in thing.calls] == ["create"]
    assert thing.calls[0][1].resource_properties.size == 3


def test_an_update_runs_update_and_leaves_out_absent_data(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes], thing: FakeThing
) -> None:
    response = handler.on_event(event("Update", PhysicalResourceId=PHYSICAL_ID), None)

    assert response == {"PhysicalResourceId": PHYSICAL_ID}
    assert [name for name, _ in thing.calls] == ["update"]


def test_a_delete_runs_delete_and_returns_the_physical_id(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes], thing: FakeThing
) -> None:
    assert handler.on_event(event("Delete", PhysicalResourceId=PHYSICAL_ID), None) == {
        "PhysicalResourceId": PHYSICAL_ID
    }
    assert [name for name, _ in thing.calls] == ["delete"]


def test_a_delete_of_a_resource_already_gone_succeeds(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes], thing: FakeThing
) -> None:
    thing.error = failure(ErrorCode.RESOURCE_NOT_FOUND)

    assert handler.on_event(event("Delete", PhysicalResourceId=PHYSICAL_ID), None) == {
        "PhysicalResourceId": PHYSICAL_ID
    }


def test_any_other_delete_error_fails_the_event(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes], thing: FakeThing
) -> None:
    thing.error = failure(ErrorCode.CONFLICT)

    with pytest.raises(CcfApiError):
        handler.on_event(event("Delete", PhysicalResourceId=PHYSICAL_ID), None)


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_a_create_or_update_is_complete_when_the_resource_is_ready(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes],
    thing: FakeThing,
    request_type: str,
) -> None:
    thing.ready = True

    assert handler.is_complete(event(request_type, PhysicalResourceId=PHYSICAL_ID), None) == {"IsComplete": True}
    assert [name for name, _ in thing.calls] == ["is_ready"]


def test_a_delete_is_complete_once_the_framework_says_not_found(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes], thing: FakeThing
) -> None:
    deleting = event("Delete", PhysicalResourceId=PHYSICAL_ID)

    assert handler.is_complete(deleting, None) == {"IsComplete": False}
    thing.error = failure(ErrorCode.RESOURCE_NOT_FOUND)
    assert handler.is_complete(deleting, None) == {"IsComplete": True}


def test_any_other_error_while_waiting_for_a_delete_fails_the_event(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes], thing: FakeThing
) -> None:
    thing.error = failure(ErrorCode.THROTTLED)

    with pytest.raises(CcfApiError):
        handler.is_complete(event("Delete", PhysicalResourceId=PHYSICAL_ID), None)


def test_a_create_token_hashes_the_stack_and_logical_ids() -> None:
    parsed = CustomResourceEvent[ThingProperties].model_validate(event("Create"))

    assert parsed.client_token() == hashlib.sha256(f"{STACK_ID}\0{LOGICAL_ID}".encode()).hexdigest()


def test_a_replacement_token_also_names_the_replaced_resource() -> None:
    parsed = CustomResourceEvent[ThingProperties].model_validate(event("Update", PhysicalResourceId=PHYSICAL_ID))

    assert parsed.client_token() == hashlib.sha256(f"{STACK_ID}\0{LOGICAL_ID}\0{PHYSICAL_ID}".encode()).hexdigest()


@pytest.mark.parametrize(
    ("old", "changed"),
    [
        (
            {
                **PROPERTIES,
                "ServiceToken": "arn:aws:lambda:us-east-1:111122223333:function:old",
            },
            False,
        ),
        ({**PROPERTIES, "Size": "4"}, True),
    ],
)
def test_properties_changed_ignores_the_service_token(old: dict[str, str], changed: bool) -> None:
    parsed = CustomResourceEvent[ThingProperties].model_validate(
        event("Update", PhysicalResourceId=PHYSICAL_ID, OldResourceProperties=old)
    )

    assert parsed.properties_changed() is changed


def test_a_misspelled_property_fails_the_event(
    handler: CustomResourceHandler[ThingProperties, ThingAttributes], thing: FakeThing
) -> None:
    with pytest.raises(ValueError, match="Sise"):
        handler.on_event(event("Create", ResourceProperties={**PROPERTIES, "Sise": "1"}), None)

    assert thing.calls == []


def test_an_update_without_a_physical_id_is_rejected() -> None:
    parsed = CustomResourceEvent[ThingProperties].model_validate(event("Update"))

    with pytest.raises(ValueError, match="PhysicalResourceId"):
        parsed.existing_id()
