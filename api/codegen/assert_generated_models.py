"""Focused runtime assertions for generated strict Pydantic boundary models."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Any

import ccf_api_models.models as models
from ccf_api_models.base import CcfModel
from pydantic import BaseModel, ValidationError

_ACCOUNT_ID = "123456789012"
_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/ccf-source"
_EXECUTION_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/ccf-execution"
_JOB_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/ccf-job"
_BUCKET_ARN = "arn:aws:s3:::ccf-destination"
_TIMESTAMP = 1748736000.0


class GeneratedModelAsserter:
    """Checks strictness and Task 2 fields on generated models."""

    def __init__(self, generated_models: ModuleType) -> None:
        self._models = generated_models

    def assert_valid(self) -> None:
        self._assert_all_models_forbid_extras()
        self._assert_complete_operation_inputs()
        self._assert_connector_contract()
        self._assert_get_job_contract()
        self._assert_document_contract()
        self._assert_common_error()
        self._assert_constraints()

    def _assert_all_models_forbid_extras(self) -> None:
        generated = [
            value
            for value in vars(self._models).values()
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value is not BaseModel
                and value.__module__ == self._models.__name__
                and not getattr(value, "__pydantic_root_model__", False)
            )
        ]
        assert generated
        assert all(model.model_config.get("extra") == "forbid" for model in generated)
        assert all(model.model_config.get("strict") is True for model in generated)
        assert all(issubclass(model, CcfModel) for model in generated)

    def _assert_complete_operation_inputs(self) -> None:
        get_job = self._models.GetCustomConnectorJobRequest.model_validate(
            {"connector_id": "connector-1", "job_id": "job-1"}
        )
        assert get_job.connector_id == "connector-1"
        assert get_job.job_id == "job-1"
        self._assert_required(self._models.GetCustomConnectorJobRequest, "connector_id")
        self._assert_required(self._models.GetCustomConnectorJobRequest, "job_id")
        self._assert_rejects_extra(self._models.GetCustomConnectorJobRequest, get_job.model_dump())

        list_jobs = self._models.ListCustomConnectorJobsRequest.model_validate(
            {
                "connector_id": "connector-1",
                "max_results": 100,
                "next_token": "next-page",
                "status": self._models.JobStatus.RUNNING,
            }
        )
        assert list_jobs.status == self._models.JobStatus.RUNNING
        assert list_jobs.max_results == 100
        assert list_jobs.next_token == "next-page"
        self._assert_required(self._models.ListCustomConnectorJobsRequest, "connector_id")
        self._assert_invalid(
            self._models.ListCustomConnectorJobsRequest,
            {"connector_id": "connector-1", "max_results": 101},
        )
        self._assert_invalid(
            self._models.ListCustomConnectorJobsRequest,
            {"connector_id": "connector-1", "status": "UNKNOWN"},
        )

        list_connectors = self._models.ListCustomConnectorsRequest.model_validate(
            {"max_results": 1, "next_token": "next-page"}
        )
        assert list_connectors.max_results == 1
        assert list_connectors.next_token == "next-page"

        configuration = json.dumps({"repositories": [{"owner": "example", "name": "one"}]})
        update = self._models.UpdateCustomConnectorRequest.model_validate(
            {
                "connector_id": "connector-1",
                "connector_configuration": configuration,
            }
        )
        assert update.connector_configuration is not None
        assert update.connector_configuration.get_secret_value() == configuration
        assert configuration not in repr(update)
        assert configuration not in update.model_dump_json()
        assert update.model_dump_for_wire() == {
            "connector_id": "connector-1",
            "connector_configuration": configuration,
        }
        self._assert_required(self._models.UpdateCustomConnectorRequest, "connector_id")
        self._assert_rejects_extra(self._models.UpdateCustomConnectorRequest, update.model_dump())

    def _assert_connector_contract(self) -> None:
        configuration = json.dumps(
            {
                "repositories": [
                    {"owner": "example", "name": "one"},
                    {"owner": "example", "name": "two"},
                ]
            }
        )
        request = self._models.CreateCustomConnectorRequest.model_validate(self._connector_payload(configuration))
        assert request.connector_configuration.get_secret_value() == configuration
        self._assert_rejects_extra(self._models.CreateCustomConnectorRequest, request.model_dump())

        response = self._models.GetCustomConnectorResponseContent.model_validate(
            {
                "connector": {
                    "connector_id": "connector-1",
                    "arn": f"arn:aws:ccf:us-east-1:{_ACCOUNT_ID}:custom-connector/connector-1",
                    "name": request.name,
                    "created_at": _TIMESTAMP,
                    "updated_at": _TIMESTAMP,
                    "status": self._models.ConnectorStatus.AVAILABLE,
                    "container_properties": request.container_properties.model_dump(),
                    "connector_configuration": configuration,
                    "source_access": {"role_arn": _ROLE_ARN, "external_id": "ccf-external-id"},
                    "schedule": request.schedule.model_dump() if request.schedule else None,
                },
                "service_reference": {
                    "endpoint": "https://ccf.example.com",
                    "region": "us-east-1",
                    "service_principal": "ccf.amazonaws.com",
                    "service_account_id": _ACCOUNT_ID,
                    "destination": {
                        "mode": self._models.DestinationMode.BEDROCK,
                        "bucket_arn": _BUCKET_ARN,
                        "object_prefix": "documents/",
                    },
                },
            }
        )
        assert response.service_reference.destination.mode == self._models.DestinationMode.BEDROCK
        self._assert_safe_and_explicit_wire_serialization(
            response,
            configuration,
            "connector",
            "connector_configuration",
        )
        self._assert_required(self._models.GetCustomConnectorResponseContent, "connector")
        self._assert_required(self._models.GetCustomConnectorResponseContent, "service_reference")

    def _assert_get_job_contract(self) -> None:
        job_configuration = json.dumps({"repository_ids": ["one", "two"], "replace": True})
        response = self._models.GetCustomConnectorJobResponseContent.model_validate(
            {
                "job": {
                    "job_id": "job-1",
                    "connector_id": "connector-1",
                    "status": self._models.JobStatus.RUNNING,
                    "job_configuration": job_configuration,
                    "created_at": _TIMESTAMP,
                    "updated_at": _TIMESTAMP,
                    "service_execution": {
                        "execution_id": "batch-job-1",
                        "execution_role_arn": _EXECUTION_ROLE_ARN,
                        "job_role_arn": _JOB_ROLE_ARN,
                        "started_at": _TIMESTAMP,
                    },
                }
            }
        )
        assert response.job.job_configuration.get_secret_value() == job_configuration
        assert response.job.service_execution is not None
        assert response.job.service_execution.execution_id == "batch-job-1"
        self._assert_safe_and_explicit_wire_serialization(response, job_configuration, "job", "job_configuration")
        self._assert_required(self._models.GetCustomConnectorJobResponseContent, "job")
        self._assert_required(self._models.JobDetail, "job_configuration")

    def _assert_document_contract(self) -> None:
        request = self._models.BatchPutCustomConnectorDocumentsRequest.model_validate(
            {
                "connector_id": "connector-1",
                "documents": [
                    {
                        "document_id": "document-1",
                        "checksum": "sha256:abc123",
                        "object_key": "documents/document-1.txt",
                        "sidecar_key": "documents/document-1.txt.metadata.json",
                    }
                ],
            }
        )
        assert request.connector_id == "connector-1"
        assert request.documents[0].object_key.endswith(".txt")
        assert request.documents[0].sidecar_key is not None

    def _assert_common_error(self) -> None:
        error_models = [
            value
            for name, value in vars(self._models).items()
            if "ExceptionResponseContent" in name and isinstance(value, type) and issubclass(value, BaseModel)
        ]
        assert not error_models
        fields = self._models.ErrorResponse.model_fields
        assert {"code", "message", "request_id"}.issubset(fields)
        assert all(fields[name].is_required() for name in ("code", "message", "request_id"))
        error = self._models.ErrorResponse.model_validate(
            {
                "code": self._models.ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                "message": "Request rejected",
                "request_id": "request-1",
            }
        )
        assert error.code == self._models.ErrorCode.RESOURCE_LIMIT_EXCEEDED

    def _assert_constraints(self) -> None:
        connector_payload = self._connector_payload("{}")
        container_payload = connector_payload["container_properties"]
        connector_payload["name"] = "x" * 65
        self._assert_invalid(self._models.CreateCustomConnectorRequest, connector_payload)
        self._assert_invalid(
            self._models.StartCustomConnectorJobRequest,
            {"connector_id": "connector-1", "job_configuration": "x" * 65537},
        )
        for region in ("us-east-1", "us-gov-west-1", "cn-north-1", "eusc-de-east-1"):
            reference = self._models.CcfServiceReference.model_validate(
                {
                    "endpoint": "https://ccf.example.com",
                    "region": region,
                    "service_principal": "ccf.amazonaws.com",
                    "service_account_id": _ACCOUNT_ID,
                    "destination": {
                        "mode": self._models.DestinationMode.BEDROCK,
                        "bucket_arn": _BUCKET_ARN,
                        "object_prefix": "documents/",
                    },
                }
            )
            assert reference.region == region
        self._assert_invalid(
            self._models.CcfServiceReference,
            {
                "endpoint": "https://ccf.example.com",
                "region": "aws-eusc",
                "service_principal": "ccf.amazonaws.com",
                "service_account_id": _ACCOUNT_ID,
                "destination": {
                    "mode": self._models.DestinationMode.BEDROCK,
                    "bucket_arn": _BUCKET_ARN,
                    "object_prefix": "documents/",
                },
            },
        )
        self._assert_invalid(self._models.ResourceRequirements, {"cpu": 0.1, "memory": 2048})
        self._assert_invalid(self._models.ResourceRequirements, {"cpu": 1.0, "memory": 2048.5})
        self._assert_invalid(self._models.ResourceRequirements, {"cpu": "1.0", "memory": 2048})
        self._assert_invalid(self._models.ResourceRequirements, {"cpu": 1.0, "memory": "2048"})
        self._assert_invalid(
            self._models.ContainerProperties,
            {**container_payload, "command": []},
        )
        self._assert_invalid(
            self._models.ContainerProperties,
            {**container_payload, "timeout": 60.5},
        )
        self._assert_invalid(
            self._models.ContainerProperties,
            {**container_payload, "timeout": "3600"},
        )
        self._assert_invalid(
            self._models.ContainerProperties,
            {**container_payload, "image_uri": "x"},
        )
        self._assert_invalid(
            self._models.ContainerProperties,
            {**container_payload, "image_uri": "repository/connector:latest"},
        )
        self._assert_invalid(
            self._models.ContainerProperties,
            {
                **container_payload,
                "image_uri": f"{_ACCOUNT_ID}.dkr.ecr.cn-north-1.amazonaws.com/connector:latest",
            },
        )
        self._assert_invalid(
            self._models.ContainerProperties,
            {
                **container_payload,
                "image_uri": f"{_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com.cn/connector:latest",
            },
        )
        digest = "a" * 64
        digest_container = self._models.ContainerProperties.model_validate(
            {
                **container_payload,
                "image_uri": f"{_ACCOUNT_ID}.dkr.ecr.us-gov-west-1.amazonaws.com/team/connector@sha256:{digest}",
            }
        )
        assert digest_container.image_uri.endswith(digest)

        path_role_arn = f"arn:aws:iam::{_ACCOUNT_ID}:role/team/connectors/ccf-source"
        source_access = self._models.SourceAccessConfiguration.model_validate(
            {"role_arn": path_role_arn, "external_id": "ccf-external-id"}
        )
        assert source_access.role_arn == path_role_arn
        self._assert_invalid(
            self._models.SourceAccessConfiguration,
            {"role_arn": f"arn:aws:iam::{_ACCOUNT_ID}:role/team connector", "external_id": "ccf-external-id"},
        )
        self._assert_invalid(
            self._models.SourceAccessConfiguration,
            {"role_arn": f"arn:aws:iam::{_ACCOUNT_ID}:role/team//connector", "external_id": "ccf-external-id"},
        )
        self._assert_invalid(
            self._models.BatchPutCustomConnectorDocumentsRequest,
            {"connector_id": "connector-1", "documents": []},
        )

    @staticmethod
    def _assert_safe_and_explicit_wire_serialization(
        model: CcfModel,
        secret: str,
        container_field: str,
        secret_field: str,
    ) -> None:
        assert secret not in repr(model)
        assert secret not in model.model_dump_json()
        wire_payload = model.model_dump_for_wire()
        nested_payload = wire_payload[container_field]
        assert isinstance(nested_payload, dict)
        assert nested_payload[secret_field] == secret
        json.dumps(wire_payload)

    @staticmethod
    def _connector_payload(configuration: str) -> dict[str, Any]:
        return {
            "name": "multi-repository-connector",
            "container_properties": {
                "image_uri": f"{_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/connector:latest",
                "command": ["ccf-connector", "run", "connector:definition"],
                "resource_requirements": {"cpu": 1.0, "memory": 2048},
                "timeout": 3600,
            },
            "connector_configuration": configuration,
            "source_access": {"role_arn": _ROLE_ARN, "external_id": "ccf-external-id"},
            "schedule": {
                "expression": "rate(1 hour)",
                "state": models.ScheduleState.ENABLED,
                "timezone": "UTC",
            },
        }

    @staticmethod
    def _assert_required(model: type[BaseModel], field_name: str) -> None:
        assert model.model_fields[field_name].is_required()

    @staticmethod
    def _assert_rejects_extra(model: type[BaseModel], payload: dict[str, Any]) -> None:
        payload["unknown_field"] = "rejected"
        GeneratedModelAsserter._assert_invalid(model, payload)

    @staticmethod
    def _assert_invalid(model: type[BaseModel], payload: dict[str, Any]) -> None:
        try:
            model.model_validate(payload)
        except ValidationError:
            return
        raise AssertionError(f"{model.__name__} accepted an invalid payload")


def main() -> None:
    openapi: Any = json.loads(Path("generated/openapi.json").read_text(encoding="utf-8"))
    schema_names = set(openapi["components"]["schemas"])
    missing_models = schema_names - vars(models).keys()
    assert not missing_models, f"generated Pydantic models missing for schemas: {sorted(missing_models)}"

    GeneratedModelAsserter(models).assert_valid()
    generated_count = sum(
        1
        for value in vars(models).values()
        if isinstance(value, type) and (issubclass(value, BaseModel) or issubclass(value, StrEnum))
    )
    print(f"OK: {generated_count} generated model and enum types verified")


if __name__ == "__main__":
    main()
