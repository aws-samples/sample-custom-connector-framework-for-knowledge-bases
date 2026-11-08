"""The read-only Get and List operations: one class each, reading member names from the operation's spec."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ccf_api_models.base import CcfModel
from ccf_api_models.catalog import OperationSpec, Pagination
from pydantic import BaseModel

from common.errors import ResourceNotFoundError
from common.operation import OperationRequest
from common.storage.ddb.model_table import ListItemsRequest, Page

type RequestRef[RefT] = Callable[[OperationRequest[Any]], RefT]
"""Builds the resource or parent reference from a request: `DestinationRef.of`, `ConnectorRef.of`, and so on."""


class ReadResource[RefT, ItemT: BaseModel]:
    def __init__(self, get: Callable[[RefT], ItemT], ref: RequestRef[RefT], spec: OperationSpec) -> None:
        self._get = get
        self._ref = ref
        self._response = _response_model(spec)

    def read(self, request: OperationRequest[Any]) -> CcfModel:
        """The response's one member holds the item; `get` answers 404 itself."""
        [member] = self._response.model_fields
        return self._response.model_validate({member: self._get(self._ref(request))}, from_attributes=True)


class ListResources[ParentT, ItemT: BaseModel]:
    def __init__(
        self,
        list_items: Callable[[ListItemsRequest[ParentT]], Page[ItemT]],
        parent: RequestRef[ParentT],
        spec: OperationSpec,
        parent_exists: Callable[[ParentT], bool] | None = None,
    ) -> None:
        if spec.pagination is None:
            raise ValueError(f"{spec.operation} isn't paginated")
        self._list_items = list_items
        self._parent = parent
        self._spec = spec
        self._pagination: Pagination = spec.pagination
        self._response = _response_model(spec)
        self._parent_exists = parent_exists

    def list(self, request: OperationRequest[Any]) -> CcfModel:
        """One page; a missing parent answers 404 unless the request carries the resolver's custom connector."""
        parent = self._parent(request)
        if self._parent_exists is not None and request.connector is None and not self._parent_exists(parent):
            raise ResourceNotFoundError(f"The parent of {self._spec.operation} does not exist")
        payload = request.payload
        filter_member = self._spec.filter_member
        filter_value = None if filter_member is None else getattr(payload, filter_member)
        page = self._list_items(
            ListItemsRequest(
                parent=parent,
                max_results=getattr(payload, self._pagination.page_size),
                next_token=getattr(payload, self._pagination.input_token),
                filter_value=None if filter_value is None else str(filter_value),
            )
        )
        return self._response.model_validate(
            {self._pagination.items: page.items, self._pagination.output_token: page.next_token}, from_attributes=True
        )


def _response_model(spec: OperationSpec) -> type[CcfModel]:
    if spec.response_model is None:
        raise ValueError(f"{spec.operation} has no response")
    return spec.response_model
