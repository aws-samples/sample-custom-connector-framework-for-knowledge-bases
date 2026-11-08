"""The `Destination` custom resource, against an in-memory framework API."""

import json
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, cast

import pytest
from ccf_api_models.base import CcfModel
from ccf_api_models.models import (
    CreateDestinationRequest,
    CreateDestinationResponseContent,
    DeleteDestinationRequest,
    DestinationType,
    ErrorCode,
    GetDestinationRequest,
    GetDestinationResponseContent,
    UpdateDestinationRequest,
)
from ccf_api_models.operations import CcfApi
from custom_connector_framework.client import ApiFailure, CcfApiError
from pydantic import ValidationError

import destination
from custom_resource import CustomResourceHandler

DESTINATION_ID = "dst-7c41e9a2b5d0"
STACK_ID = "arn:aws:cloudformation:us-east-1:111122223333:stack/GitHubTenant/0f1e2d3c"
LOGICAL_ID = "TenantDestination238C44EF"
BUCKET_NAME = "ccf-dst-7c41e9a2b5d0-111122223333-us-east-1"
KNOWLEDGE_BASE_ID = "KB12345678"
DATA_SOURCE_ID = "DS12345678"
CONFLICT_MESSAGE = "Destination still contains a custom connector"
# Read-only, so no test can change what the next one sends; each event gets its own copy.
PROPERTIES: Mapping[str, Any] = MappingProxyType(
    {
        "ServiceToken": "arn:aws:lambda:us-east-1:111122223333:function:provider",
        "Name": "github-open-source",
        "Type": "BEDROCK",
        "RoleArn": "arn:aws:iam::111122223333:role/ccf/destinations/CcfDestinationRole-GitHub",
        # CloudFormation sends every boolean as a string.
        "AccessControlEnabled": "false",
        "MetadataFields": [],
    }
)
OWNER = {"Name": "owner", "Type": "STRING", "Searchability": "ENABLED"}


def _api_error(status_code: int, code: ErrorCode, message: str) -> CcfApiError:
    return CcfApiError(ApiFailure(status_code=status_code, code=code, message=message, request_id="req-1"))


class FakeCcfApi:
    """The destination operations `destination.py` calls; each test sets the destination's status."""

    def __init__(self) -> None:
        self.created: list[CreateDestinationRequest] = []
        self.updated: list[UpdateDestinationRequest] = []
        self.deleted: list[DeleteDestinationRequest] = []
        self.destination_type = "BEDROCK"
        self.status = "CREATING_BUCKET"
        self.status_reason: str | None = None
        self.metadata_fields: list[dict[str, str]] = []
        self.updating = False
        """The knowledge base is still applying an earlier update, so `UpdateDestination` conflicts."""
        self.exists = True
        self.has_connectors = False
        self._handler = CustomResourceHandler(
            destination.DestinationCalls(cast(CcfApi, self)), destination.DestinationProperties
        )

    def call(self, request: CcfModel) -> Any:
        answers: dict[type[CcfModel], Callable[[Any], Any]] = {
            CreateDestinationRequest: self._create,
            GetDestinationRequest: self._get,
            UpdateDestinationRequest: self._update,
            DeleteDestinationRequest: self._delete,
        }
        return answers[type(request)](request)

    def _update(self, request: UpdateDestinationRequest) -> None:
        self.updated.append(request)
        if self.updating:
            raise _api_error(409, ErrorCode.CONFLICT, "The knowledge base is still applying an earlier update")
        self.metadata_fields = [field.model_dump(mode="json") for field in request.metadata_fields]

    def _create(self, request: CreateDestinationRequest) -> CreateDestinationResponseContent:
        self.created.append(request)
        self.destination_type = request.type.value
        return CreateDestinationResponseContent.model_validate_json(json.dumps({"destination": self._summary()}))

    def _get(self, request: GetDestinationRequest) -> GetDestinationResponseContent:
        if not self.exists:
            raise _api_error(404, ErrorCode.RESOURCE_NOT_FOUND, "not found")
        return GetDestinationResponseContent.model_validate_json(json.dumps({"destination": self._destination()}))

    def _delete(self, request: DeleteDestinationRequest) -> None:
        self.deleted.append(request)
        if self.has_connectors:
            raise _api_error(409, ErrorCode.CONFLICT, CONFLICT_MESSAGE)
        self.status = "DELETING"

    def on_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Run `onEvent` against this API and return its response."""
        return self._handler.on_event(event, None)

    def is_complete(self, event: dict[str, Any]) -> dict[str, Any]:
        """Run `isComplete` against this API and return its response."""
        return self._handler.is_complete(event, None)

    def _summary(self) -> dict[str, Any]:
        return {
            "destination_id": DESTINATION_ID,
            "arn": f"arn:aws:ccf:us-east-1:111122223333:destination/{DESTINATION_ID}",
            "name": PROPERTIES["Name"],
            "type": self.destination_type,
            "access_control_enabled": False,
            "status": self.status,
            "created_at": 1.0,
            "updated_at": 1.0,
        }

    def _destination(self) -> dict[str, Any]:
        resources: dict[str, Any] = {}
        if self.status == "ACTIVE":
            resources["bucket_arn"] = f"arn:aws:s3:::{BUCKET_NAME}"
            resources["bucket_name"] = BUCKET_NAME
            if self.destination_type == "BEDROCK":
                resources["knowledge_base_arn"] = (
                    f"arn:aws:bedrock:us-east-1:111122223333:knowledge-base/{KNOWLEDGE_BASE_ID}"
                )
                resources["knowledge_base_id"] = KNOWLEDGE_BASE_ID
                resources["data_source_id"] = DATA_SOURCE_ID
        return {
            **self._summary(),
            "status_reason": self.status_reason,
            "role_arn": PROPERTIES["RoleArn"],
            "destination_resources": resources,
            "metadata_fields": self.metadata_fields,
        }


def _event(request_type: str) -> dict[str, Any]:
    return {
        "RequestType": request_type,
        "StackId": STACK_ID,
        "LogicalResourceId": LOGICAL_ID,
        "ResourceProperties": {**PROPERTIES},
    }


def _delete_event() -> dict[str, Any]:
    return {**_event("Delete"), "PhysicalResourceId": DESTINATION_ID}


def _replacement_event() -> dict[str, Any]:
    return {
        **_event("Update"),
        "PhysicalResourceId": DESTINATION_ID,
        "OldResourceProperties": {**PROPERTIES, "Name": "old-name"},
    }


@pytest.fixture
def api() -> FakeCcfApi:
    return FakeCcfApi()


def test_create_sends_the_name_type_role_and_access_control(api: FakeCcfApi) -> None:
    api.on_event(_event("Create"))

    [request] = api.created
    assert (request.name, request.type, request.role_arn, request.access_control_enabled) == (
        "github-open-source",
        DestinationType.BEDROCK,
        PROPERTIES["RoleArn"],
        False,
    )


def test_a_retried_create_sends_the_same_client_token(api: FakeCcfApi) -> None:
    api.on_event(_event("Create"))
    api.on_event(_event("Create"))

    first, retried = api.created
    assert first.client_token == retried.client_token


def test_a_replacement_sends_a_different_client_token_than_the_create(api: FakeCcfApi) -> None:
    api.on_event(_event("Create"))
    api.on_event(_replacement_event())

    create, replacement = api.created
    assert create.client_token != replacement.client_token


def test_create_sends_access_control_when_enabled(api: FakeCcfApi) -> None:
    api.on_event(
        {
            **_event("Create"),
            "ResourceProperties": {**PROPERTIES, "AccessControlEnabled": "true"},
        },
    )

    assert api.created[0].access_control_enabled is True


def test_create_returns_the_destination_id_before_the_destination_is_active(
    api: FakeCcfApi,
) -> None:
    assert api.on_event(_event("Create")) == {
        "PhysicalResourceId": DESTINATION_ID,
        "Data": {
            "DestinationId": DESTINATION_ID,
            "BucketName": "",
            "KnowledgeBaseId": "",
            "DataSourceId": "",
        },
    }


def test_create_is_not_complete_while_the_workflow_runs(api: FakeCcfApi) -> None:
    assert api.is_complete({**_event("Create"), "PhysicalResourceId": DESTINATION_ID}) == {"IsComplete": False}


def test_create_completes_at_active_with_the_bucket_and_knowledge_base(
    api: FakeCcfApi,
) -> None:
    api.status = "ACTIVE"

    assert api.is_complete({**_event("Create"), "PhysicalResourceId": DESTINATION_ID}) == {
        "IsComplete": True,
        "Data": {
            "DestinationId": DESTINATION_ID,
            "BucketName": BUCKET_NAME,
            "KnowledgeBaseId": KNOWLEDGE_BASE_ID,
            "DataSourceId": DATA_SOURCE_ID,
        },
    }


def test_an_s3_destination_returns_empty_knowledge_base_ids_from_on_event_and_is_complete(
    api: FakeCcfApi,
) -> None:
    event = {**_event("Create"), "ResourceProperties": {**PROPERTIES, "Type": "S3"}}
    created = api.on_event(event)
    api.status = "ACTIVE"

    completed = api.is_complete({**event, "PhysicalResourceId": DESTINATION_ID})

    for data in (created["Data"], completed["Data"]):
        assert (data["KnowledgeBaseId"], data["DataSourceId"]) == ("", "")
    assert completed["Data"]["BucketName"] == BUCKET_NAME


def test_a_failed_destination_fails_the_resource_with_its_status_reason(
    api: FakeCcfApi,
) -> None:
    api.status = "FAILED"
    api.status_reason = "CreateBucket: AccessDenied (correlation 0f1e2d3c)"

    with pytest.raises(RuntimeError, match="AccessDenied"):
        api.is_complete({**_event("Create"), "PhysicalResourceId": DESTINATION_ID})


def _with_owner(event: dict[str, Any]) -> dict[str, Any]:
    return {**event, "ResourceProperties": {**PROPERTIES, "MetadataFields": [OWNER]}}


def test_create_sets_the_metadata_fields_once_the_destination_is_active(api: FakeCcfApi) -> None:
    event = _with_owner({**_event("Create"), "PhysicalResourceId": DESTINATION_ID})
    api.on_event(event)
    assert api.is_complete(event) == {"IsComplete": False}
    assert api.updated == []
    api.status = "ACTIVE"

    assert api.is_complete(event)["IsComplete"] is True

    [update] = api.updated
    assert (update.destination_id, [field.name for field in update.metadata_fields]) == (DESTINATION_ID, ["owner"])


def test_a_create_waits_while_the_knowledge_base_applies_an_earlier_update(api: FakeCcfApi) -> None:
    event = _with_owner({**_event("Create"), "PhysicalResourceId": DESTINATION_ID})
    api.status, api.updating = "ACTIVE", True

    assert api.is_complete(event) == {"IsComplete": False}
    api.updating = False
    assert api.is_complete(event)["IsComplete"] is True


def test_completion_with_the_fields_already_set_makes_no_update(api: FakeCcfApi) -> None:
    event = _with_owner({**_event("Create"), "PhysicalResourceId": DESTINATION_ID})
    api.status = "ACTIVE"
    api.metadata_fields = [{"name": "owner", "type": "STRING", "searchability": "ENABLED"}]

    assert api.is_complete(event)["IsComplete"] is True
    assert api.updated == []


def test_an_update_of_only_the_metadata_fields_keeps_the_destination_and_sets_them(api: FakeCcfApi) -> None:
    api.status = "ACTIVE"
    event = _with_owner(
        {**_event("Update"), "PhysicalResourceId": DESTINATION_ID, "OldResourceProperties": {**PROPERTIES}}
    )

    response = api.on_event(event)
    api.is_complete(event)

    assert (api.created, response["PhysicalResourceId"]) == ([], DESTINATION_ID)
    assert [field.name for field in api.updated[0].metadata_fields] == ["owner"]


def test_an_update_of_the_metadata_fields_and_the_name_replaces_the_destination(api: FakeCcfApi) -> None:
    api.on_event(_with_owner(_replacement_event()))

    assert len(api.created) == 1
    assert api.updated == []


def test_an_update_from_a_stack_without_metadata_fields_to_none_keeps_the_destination(api: FakeCcfApi) -> None:
    api.status = "ACTIVE"
    old = {key: value for key, value in PROPERTIES.items() if key != "MetadataFields"}

    api.on_event({**_event("Update"), "PhysicalResourceId": DESTINATION_ID, "OldResourceProperties": old})

    assert (api.created, api.updated) == ([], [])


def test_an_update_with_unchanged_properties_keeps_the_destination(
    api: FakeCcfApi,
) -> None:
    api.status = "ACTIVE"

    response = api.on_event(
        {
            **_event("Update"),
            "PhysicalResourceId": DESTINATION_ID,
            "OldResourceProperties": {
                **PROPERTIES,
                "ServiceToken": "arn:aws:lambda:us-east-1:111122223333:function:old",
            },
        },
    )

    assert api.created == []
    assert response["Data"]["BucketName"] == BUCKET_NAME


def test_delete_makes_no_api_call_and_returns_the_physical_id(api: FakeCcfApi) -> None:
    api.status = "ACTIVE"

    response = api.on_event(_delete_event())

    assert response == {"PhysicalResourceId": DESTINATION_ID}
    assert api.deleted == []


def test_delete_of_an_active_destination_deletes_it_and_completes_at_not_found(api: FakeCcfApi) -> None:
    api.status = "ACTIVE"
    api.on_event(_delete_event())

    assert api.is_complete(_delete_event()) == {"IsComplete": False}
    assert api.deleted == [DeleteDestinationRequest(destination_id=DESTINATION_ID)]
    api.exists = False
    assert api.is_complete(_delete_event()) == {"IsComplete": True}


def test_delete_while_the_destination_is_deleting_waits_without_another_call(api: FakeCcfApi) -> None:
    api.status = "DELETING"

    assert api.is_complete(_delete_event()) == {"IsComplete": False}
    assert api.deleted == []


def test_a_rollback_after_a_create_timed_out_waits_for_the_create_then_deletes(api: FakeCcfApi) -> None:
    api.on_event(_event("Create"))
    api.on_event(_delete_event())

    assert api.is_complete(_delete_event()) == {"IsComplete": False}
    assert api.deleted == []
    api.status = "ACTIVE"
    assert api.is_complete(_delete_event()) == {"IsComplete": False}
    assert len(api.deleted) == 1
    api.exists = False
    assert api.is_complete(_delete_event()) == {"IsComplete": True}


def test_a_rollback_after_a_failed_create_deletes_and_completes_at_not_found(api: FakeCcfApi) -> None:
    api.status = "FAILED"
    api.on_event(_delete_event())

    assert api.is_complete(_delete_event()) == {"IsComplete": False}
    assert api.deleted == [DeleteDestinationRequest(destination_id=DESTINATION_ID)]
    api.exists = False
    assert api.is_complete(_delete_event()) == {"IsComplete": True}


def test_a_delete_refused_for_registered_connectors_fails_the_event_with_the_api_message(
    api: FakeCcfApi,
) -> None:
    api.status = "ACTIVE"
    api.has_connectors = True

    with pytest.raises(CcfApiError, match=CONFLICT_MESSAGE):
        api.is_complete(_delete_event())


def test_delete_of_a_destination_already_gone_succeeds(api: FakeCcfApi) -> None:
    api.exists = False

    assert api.on_event(_delete_event())["PhysicalResourceId"] == DESTINATION_ID
    assert api.is_complete(_delete_event()) == {"IsComplete": True}


def test_a_misspelled_property_fails_the_event_instead_of_being_dropped(
    api: FakeCcfApi,
) -> None:
    event = {
        **_event("Create"),
        "ResourceProperties": {**PROPERTIES, "AccessControlEnable": "true"},
    }

    with pytest.raises(ValidationError, match="AccessControlEnable"):
        api.on_event(event)

    assert api.created == []
