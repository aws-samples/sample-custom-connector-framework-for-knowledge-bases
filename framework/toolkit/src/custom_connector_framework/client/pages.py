"""Every page of a list operation, each call resent with the previous page's `next_token`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, Self

from ccf_api_models.base import ValueModel
from ccf_api_models.models import (
    ListCustomConnectorCollectionsRequest,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorJobsRequest,
    ListCustomConnectorsRequest,
    ListDestinationsRequest,
)
from ccf_api_models.operations import CcfApi
from pydantic import ConfigDict, SkipValidation

type ListRequest = (
    ListDestinationsRequest
    | ListCustomConnectorsRequest
    | ListCustomConnectorJobsRequest
    | ListCustomConnectorDocumentsRequest
    | ListCustomConnectorCollectionsRequest
)


class PagedResponse(Protocol):
    """A generated list response: its items and the token of the next page."""

    @property
    def next_token(self) -> str | None: ...

    @classmethod
    def model_validate(cls, obj: Any) -> Self: ...


class PagesRequest[ResponseT: PagedResponse](ValueModel):
    """A list request and the response model its operation returns."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    request: ListRequest
    response_type: SkipValidation[type[ResponseT]]
    """Not validated: a protocol bound has no runtime class check."""


class Pages:
    def __init__(self, api: CcfApi) -> None:
        self._api = api

    def of[ResponseT: PagedResponse](self, request: PagesRequest[ResponseT]) -> Iterator[ResponseT]:
        """Yield each page until one has no `next_token`."""
        current = request.request
        while True:
            page = request.response_type.model_validate(self._api.call(current))
            yield page
            if page.next_token is None:
                return
            current = current.model_copy(update={"next_token": page.next_token})
