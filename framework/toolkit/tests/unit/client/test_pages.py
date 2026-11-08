"""Paging through a list operation."""

from __future__ import annotations

from typing import cast

from ccf_api_models.base import CcfModel
from ccf_api_models.models import ListCustomConnectorDocumentsRequest, ListCustomConnectorDocumentsResponseContent
from ccf_api_models.operations import CcfApi

from custom_connector_framework.client.pages import Pages, PagesRequest

REQUEST = ListCustomConnectorDocumentsRequest(
    destination_id="dst-7c41e9a2b5d0", connector_id="cc-e020d65d76c0", collection_id="col-default", max_results=2
)


class PagedApi:
    """Answers each list call with the next of the given tokens, and records the requests."""

    def __init__(self, tokens: list[str | None]) -> None:
        self._tokens = tokens
        self.requests: list[ListCustomConnectorDocumentsRequest] = []

    def call(self, request: CcfModel) -> ListCustomConnectorDocumentsResponseContent:
        assert isinstance(request, ListCustomConnectorDocumentsRequest)
        self.requests.append(request)
        return ListCustomConnectorDocumentsResponseContent(
            documents=[], next_token=self._tokens[len(self.requests) - 1]
        )


def pages(api: PagedApi) -> list[ListCustomConnectorDocumentsResponseContent]:
    return list(
        Pages(cast(CcfApi, api)).of(
            PagesRequest(request=REQUEST, response_type=ListCustomConnectorDocumentsResponseContent)
        )
    )


def test_a_three_page_listing_yields_three_responses_in_order() -> None:
    api = PagedApi(["page-2", "page-3", None])

    listed = pages(api)

    assert [page.next_token for page in listed] == ["page-2", "page-3", None]
    assert [request.next_token for request in api.requests] == [None, "page-2", "page-3"]


def test_a_one_page_listing_makes_one_call() -> None:
    api = PagedApi([None])

    assert len(pages(api)) == 1
    assert len(api.requests) == 1


def test_each_resend_keeps_the_requests_other_members() -> None:
    api = PagedApi(["page-2", None])

    pages(api)

    assert [request.model_dump(exclude={"next_token"}) for request in api.requests] == [
        REQUEST.model_dump(exclude={"next_token"})
    ] * 2
