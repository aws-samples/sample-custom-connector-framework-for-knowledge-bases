"""The connector image build custom resource, against an in-memory CodeBuild and ECR."""

from typing import Any

import pytest
from botocore.exceptions import ClientError

import image_build
from custom_resource import CustomResourceHandler

PROJECT = "ConnectorImageBuild-AbCdEf"
REPOSITORY = "ccf-connectors/github"
IMAGE_TAG = "0a1b2c"
IMAGE_URI = f"111122223333.dkr.ecr.us-east-1.amazonaws.com/{REPOSITORY}:{IMAGE_TAG}"
BUILD_ID = f"{PROJECT}:0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


class FakeCodeBuild:
    """Starts builds and reports one status for every build it is asked about."""

    def __init__(self, status: str = "IN_PROGRESS") -> None:
        self.started: list[str] = []
        self.read: list[str] = []
        self.status = status

    def start_build(self, *, projectName: str) -> dict[str, Any]:  # noqa: N803
        self.started.append(projectName)
        return {"build": {"id": BUILD_ID}}

    def batch_get_builds(self, *, ids: list[str]) -> dict[str, Any]:
        self.read.extend(ids)
        return {"builds": [{"id": build_id, "buildStatus": self.status} for build_id in ids]}


class FakeEcr:
    """Holds a set of (repository, tag) pairs and answers DescribeImages the way ECR does."""

    def __init__(self, tags: set[tuple[str, str]] | None = None, error_code: str = "") -> None:
        self.tags = tags or set()
        self.error_code = error_code
        self.described: list[tuple[str, str]] = []

    def describe_images(self, *, repositoryName: str, imageIds: list[dict[str, str]]) -> dict[str, Any]:  # noqa: N803
        [image_id] = imageIds
        self.described.append((repositoryName, image_id["imageTag"]))
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code}}, "DescribeImages")
        if (repositoryName, image_id["imageTag"]) not in self.tags:
            raise ClientError({"Error": {"Code": "ImageNotFoundException"}}, "DescribeImages")
        return {"imageDetails": [{"imageTags": [image_id["imageTag"]]}]}


class Resource:
    """The handler over the fakes, run the way the Provider framework runs it."""

    def __init__(self, codebuild: FakeCodeBuild | None = None, ecr: FakeEcr | None = None) -> None:
        self.codebuild = codebuild or FakeCodeBuild()
        self.ecr = ecr or FakeEcr()
        self._handler = CustomResourceHandler(
            image_build.ImageBuild(self.codebuild, self.ecr), image_build.ImageBuildProperties
        )

    def on_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return self._handler.on_event(event, None)

    def is_complete(self, event: dict[str, Any]) -> dict[str, Any]:
        return self._handler.is_complete(event, None)


def event(request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "RequestType": request_type,
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/GitHub/0f1e2d3c",
        "LogicalResourceId": "ConnectorImage",
        "ResourceProperties": {
            "ServiceToken": "arn:aws:lambda:us-east-1:111122223333:function:provider",
            "ProjectName": PROJECT,
            "RepositoryName": REPOSITORY,
            "ImageTag": IMAGE_TAG,
            "ImageUri": IMAGE_URI,
        },
    }
    if request_type != "Create":
        raw["PhysicalResourceId"] = IMAGE_URI
    if data is not None:
        raw["Data"] = data
    return raw


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_a_missing_tag_starts_one_build_and_names_the_image(request_type: str) -> None:
    resource = Resource()

    response = resource.on_event(event(request_type))

    assert resource.ecr.described == [(REPOSITORY, IMAGE_TAG)]
    assert resource.codebuild.started == [PROJECT]
    assert response == {"PhysicalResourceId": IMAGE_URI, "Data": {"BuildId": BUILD_ID}}


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_a_tag_the_repository_holds_starts_no_build(request_type: str) -> None:
    resource = Resource(ecr=FakeEcr({(REPOSITORY, IMAGE_TAG)}))

    response = resource.on_event(event(request_type))

    assert resource.codebuild.started == []
    assert response == {"PhysicalResourceId": IMAGE_URI, "Data": {"ImageUri": IMAGE_URI}}


def test_a_tag_the_repository_holds_is_complete_without_reading_a_build() -> None:
    resource = Resource(codebuild=FakeCodeBuild("FAILED"), ecr=FakeEcr({(REPOSITORY, IMAGE_TAG)}))
    started = resource.on_event(event("Update"))

    completed = resource.is_complete(event("Update", started["Data"]))

    assert completed == {"IsComplete": True, "Data": {"ImageUri": IMAGE_URI}}
    assert resource.codebuild.read == []


def test_another_ecr_error_raises_and_starts_no_build() -> None:
    resource = Resource(ecr=FakeEcr(error_code="AccessDeniedException"))

    with pytest.raises(ClientError, match="AccessDeniedException"):
        resource.on_event(event("Create"))

    assert resource.codebuild.started == []


def test_a_delete_removes_nothing_and_is_complete_at_once() -> None:
    resource = Resource(codebuild=FakeCodeBuild("FAILED"))

    response = resource.on_event(event("Delete"))

    assert response == {"PhysicalResourceId": IMAGE_URI}
    assert (resource.codebuild.started, resource.ecr.described) == ([], [])
    assert resource.is_complete(event("Delete")) == {"IsComplete": True}


def test_a_running_build_is_not_complete() -> None:
    resource = Resource(codebuild=FakeCodeBuild("IN_PROGRESS"))

    assert resource.is_complete(event("Create", {"BuildId": BUILD_ID})) == {"IsComplete": False}


def test_a_succeeded_build_completes_with_the_image_uri() -> None:
    resource = Resource(codebuild=FakeCodeBuild("SUCCEEDED"))

    assert resource.is_complete(event("Create", {"BuildId": BUILD_ID})) == {
        "IsComplete": True,
        "Data": {"ImageUri": IMAGE_URI},
    }


@pytest.mark.parametrize("status", ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"])
def test_a_build_that_ends_otherwise_fails_the_resource_naming_the_build_and_project(status: str) -> None:
    resource = Resource(codebuild=FakeCodeBuild(status))

    with pytest.raises(RuntimeError, match=f"{BUILD_ID} of {PROJECT} ended {status}"):
        resource.is_complete(event("Create", {"BuildId": BUILD_ID}))
