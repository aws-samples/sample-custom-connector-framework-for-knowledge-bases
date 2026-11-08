"""The custom resource that builds a connector image with CodeBuild and waits until the image is pushed.

A create or update starts one build of the image's CodeBuild project unless the repository already holds
the image tag, as on a rollback to an earlier source: its tags are immutable, so a second push of the tag
would fail the stack. Deleting the resource removes nothing: the repository's lifecycle rule expires old
images.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, cast

import boto3
from botocore.exceptions import ClientError

from custom_resource import (
    CompletionResponse,
    CustomResourceEvent,
    CustomResourceHandler,
    CustomResourceModel,
    CustomResourceProperties,
    CustomResourceResponse,
)


class BuildStatus(StrEnum):
    """Every CodeBuild build status."""

    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    FAULT = "FAULT"
    STOPPED = "STOPPED"
    TIMED_OUT = "TIMED_OUT"


class EcrErrorCode(StrEnum):
    IMAGE_NOT_FOUND = "ImageNotFoundException"


class CodeBuildClient(Protocol):
    def start_build(self, *, projectName: str) -> dict[str, Any]: ...  # noqa: N803

    def batch_get_builds(self, *, ids: list[str]) -> dict[str, Any]: ...


class EcrClient(Protocol):
    def describe_images(self, *, repositoryName: str, imageIds: list[dict[str, str]]) -> dict[str, Any]: ...  # noqa: N803


class ImageBuildProperties(CustomResourceProperties):
    project_name: str
    repository_name: str
    image_tag: str
    image_uri: str


class ImageBuildAttributes(CustomResourceModel):
    """`BuildId` from `on_event` when a build started; `ImageUri` once the image is in the repository."""

    build_id: str | None = None
    image_uri: str | None = None


type ImageBuildEvent = CustomResourceEvent[ImageBuildProperties]


class ImageBuild:
    """Starts a build for a tag the repository doesn't hold, and polls it."""

    def __init__(self, codebuild: CodeBuildClient, ecr: EcrClient) -> None:
        self._codebuild = codebuild
        self._ecr = ecr

    def create(self, event: ImageBuildEvent) -> CustomResourceResponse[ImageBuildAttributes]:
        properties = event.resource_properties
        if self._tag_exists(properties):
            attributes = ImageBuildAttributes(image_uri=properties.image_uri)
        else:
            build = self._codebuild.start_build(projectName=properties.project_name)["build"]
            attributes = ImageBuildAttributes(build_id=build["id"])
        return CustomResourceResponse(physical_resource_id=properties.image_uri, data=attributes)

    def update(self, event: ImageBuildEvent) -> CustomResourceResponse[ImageBuildAttributes]:
        return self.create(event)

    def delete(self, event: ImageBuildEvent) -> None:
        """No call: the repository's lifecycle rule expires the image."""

    def is_ready(self, event: ImageBuildEvent) -> CompletionResponse[ImageBuildAttributes]:
        """Complete once the build succeeds, or at once when no build started; any other end raises."""
        properties = event.resource_properties
        done = CompletionResponse[ImageBuildAttributes](
            is_complete=True, data=ImageBuildAttributes(image_uri=properties.image_uri)
        )
        build_id = event.data.get("BuildId")
        if build_id is None:
            return done
        [build] = self._codebuild.batch_get_builds(ids=[build_id])["builds"]
        match BuildStatus(build["buildStatus"]):
            case BuildStatus.IN_PROGRESS:
                return CompletionResponse(is_complete=False)
            case BuildStatus.SUCCEEDED:
                return done
            case status:
                raise RuntimeError(
                    f"The image build {build_id} of {properties.project_name} ended {status}; "
                    "see the project's log group."
                )

    def is_gone(self, event: ImageBuildEvent) -> bool:
        return True

    def _tag_exists(self, properties: ImageBuildProperties) -> bool:
        try:
            self._ecr.describe_images(
                repositoryName=properties.repository_name, imageIds=[{"imageTag": properties.image_tag}]
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == EcrErrorCode.IMAGE_NOT_FOUND:
                return False
            raise
        return True


_HANDLER = CustomResourceHandler(
    ImageBuild(cast("CodeBuildClient", boto3.client("codebuild")), cast("EcrClient", boto3.client("ecr"))),
    ImageBuildProperties,
)


def on_event(event: dict[str, object], context: object) -> dict[str, object]:
    return _HANDLER.on_event(event, context)


def is_complete(event: dict[str, object], context: object) -> dict[str, object]:
    return _HANDLER.is_complete(event, context)
