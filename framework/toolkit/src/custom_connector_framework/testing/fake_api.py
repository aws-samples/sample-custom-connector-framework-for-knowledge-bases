"""An in-memory framework API that answers the calls a job makes.

`FakeCcfApi` holds documents, collections, and checkpoints and rejects what the framework
rejects: a `document_id` or `collection_id` that isn't the hash of its source ID, a collection
other than `col-default` without its source ID, and `col-default` with one. Batch sizes and
checkpoint sizes are checked by the request models themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ccf_api_models.base import CcfModel
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.identifiers import CollectionIdentity, DocumentKey, SourceIdKind
from ccf_api_models.models import (
    BatchDeleteCustomConnectorCollectionsRequest,
    BatchDeleteCustomConnectorDocumentsRequest,
    BatchPutCustomConnectorDocumentsRequest,
    Checkpoint,
    Collection,
    ErrorCode,
    GetCustomConnectorCheckpointRequest,
    GetCustomConnectorCheckpointResponseContent,
    GetCustomConnectorJobRequest,
    GetCustomConnectorJobResponseContent,
    GetCustomConnectorRequest,
    GetCustomConnectorResponseContent,
    GetDestinationRequest,
    GetDestinationResponseContent,
    ListCustomConnectorCollectionsRequest,
    ListCustomConnectorCollectionsResponseContent,
    ListCustomConnectorDocumentsRequest,
    ListCustomConnectorDocumentsResponseContent,
    PutCustomConnectorCheckpointRequest,
    StoredDocument,
)
from ccf_api_models.operations import Operation
from pydantic import BaseModel, ConfigDict

from custom_connector_framework.client import ApiFailure, CcfApiError
from custom_connector_framework.runner.job_inputs_reader import JobInputs
from custom_connector_framework.testing.fake_s3 import FakeS3

_FAKE_TIME = 1_700_000_000.0
_REQUEST_ID = "fake-request"
_CATALOG = OperationCatalog.load()


class _CollectionWrite(BaseModel):
    model_config = ConfigDict(frozen=True)

    collection_id: str
    source_collection_id: str | None

    def checked_collection_id(self) -> str:
        """The framework's rule: the collection ID is the one `CollectionIdentity` gives the source ID."""
        if CollectionIdentity.of(self.source_collection_id).collection_id != self.collection_id:
            raise _Failures.rejected("collection_id must be the collection of source_collection_id")
        return self.collection_id


class _Page[ItemT](BaseModel):
    items: list[ItemT]
    next_token: str | None
    page_size: int

    def page(self) -> _Page[ItemT]:
        """The `page_size` items the token points at, and the token of the next page."""
        start = int(self.next_token or "0")
        end = start + self.page_size
        return _Page[ItemT](
            items=self.items[start:end],
            next_token=str(end) if end < len(self.items) else None,
            page_size=self.page_size,
        )


class _Failures:
    @staticmethod
    def rejected(message: str) -> CcfApiError:
        return CcfApiError(
            ApiFailure(status_code=400, code=ErrorCode.BAD_REQUEST, message=message, request_id=_REQUEST_ID)
        )

    @staticmethod
    def not_found(message: str) -> CcfApiError:
        return CcfApiError(
            ApiFailure(status_code=404, code=ErrorCode.RESOURCE_NOT_FOUND, message=message, request_id=_REQUEST_ID)
        )

    @staticmethod
    def conflict(message: str) -> CcfApiError:
        return CcfApiError(
            ApiFailure(status_code=409, code=ErrorCode.CONFLICT, message=message, request_id=_REQUEST_ID)
        )


class FakeCcfApi:
    """The framework API operations a job makes, held in memory, with each call's operation name recorded.

    `call` answers what a job calls; any other operation raises `BAD_REQUEST`.
    """

    def __init__(self, page_size: int = 50) -> None:
        self.page_size = page_size
        """Items per page of every list operation, so a store that ignores `next_token` misses items."""
        self.job_inputs: JobInputs | None = None
        """What the job-input reads return; leave unset when the test doesn't run through `JobRunner`."""
        self.documents: dict[tuple[str, str], StoredDocument] = {}
        """By `(collection_id, document_id)`."""
        self.collections: dict[str, Collection] = {}
        self.calls: list[str] = []
        self.requests: list[CcfModel] = []
        """Each call's request, in order, beside its name in `calls`."""
        self.s3 = FakeS3()
        """The destination bucket the framework deletes a document's files from, as its batch delete does."""
        self.failures: dict[Operation, CcfApiError] = {}
        """The error each named operation raises instead of its answer, as a framework outage or throttle would."""

    def call(self, request: CcfModel) -> Any:
        """Answer the request as the framework would, recording its operation's name."""
        answers: dict[type[CcfModel], Callable[[Any], Any]] = {
            GetCustomConnectorJobRequest: self._job,
            GetCustomConnectorRequest: self._connector,
            GetDestinationRequest: self._destination,
            BatchPutCustomConnectorDocumentsRequest: self._put_documents,
            BatchDeleteCustomConnectorDocumentsRequest: self._delete_documents,
            ListCustomConnectorDocumentsRequest: self._list_documents,
            ListCustomConnectorCollectionsRequest: self._list_collections,
            BatchDeleteCustomConnectorCollectionsRequest: self._delete_collections,
            PutCustomConnectorCheckpointRequest: self._put_checkpoint,
            GetCustomConnectorCheckpointRequest: self._get_checkpoint,
        }
        operation = _CATALOG.spec_for(type(request)).operation
        answer = answers.get(type(request))
        if answer is None:
            raise _Failures.rejected(f"The fake framework doesn't answer {operation.value}")
        self.calls.append(operation.value)
        self.requests.append(request)
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure
        return answer(request)

    def _job(self, request: GetCustomConnectorJobRequest) -> GetCustomConnectorJobResponseContent:
        return GetCustomConnectorJobResponseContent(job=self._inputs().job)

    def _connector(self, request: GetCustomConnectorRequest) -> GetCustomConnectorResponseContent:
        return GetCustomConnectorResponseContent(connector=self._inputs().connector)

    def _destination(self, request: GetDestinationRequest) -> GetDestinationResponseContent:
        return GetDestinationResponseContent(destination=self._inputs().destination)

    def _put_documents(self, request: BatchPutCustomConnectorDocumentsRequest) -> None:
        write = _CollectionWrite(collection_id=request.collection_id, source_collection_id=request.source_collection_id)
        collection_id = write.checked_collection_id()
        for record in request.documents:
            if SourceIdKind.DOCUMENT.of(record.source_document_id) != record.document_id:
                raise _Failures.rejected("document_id must be doc- plus the hash of source_document_id")
        self._ensure_collection(write)
        for record in request.documents:
            stored = self.documents.get((collection_id, record.document_id))
            self.documents[(collection_id, record.document_id)] = StoredDocument(
                collection_id=collection_id,
                document_id=record.document_id,
                source_document_id=record.source_document_id,
                content_hash=record.content_hash,
                source_version=record.source_version,
                created_at=_FAKE_TIME if stored is None else stored.created_at,
                updated_at=_FAKE_TIME,
            )

    def _delete_documents(self, request: BatchDeleteCustomConnectorDocumentsRequest) -> None:
        collection_id = request.collection_id
        for document_id in request.document_ids:
            self.s3.remove_under(
                DocumentKey(
                    connector_id=request.connector_id, collection_id=collection_id, document_id=document_id
                ).prefix
            )
            self.documents.pop((collection_id, document_id), None)

    def _list_documents(
        self, request: ListCustomConnectorDocumentsRequest
    ) -> ListCustomConnectorDocumentsResponseContent:
        stored = sorted(
            (
                document
                for document in self.documents.values()
                if request.collection_id is None or document.collection_id == request.collection_id
            ),
            key=lambda document: (document.collection_id, document.document_id),
        )
        page = _Page[StoredDocument](items=stored, next_token=request.next_token, page_size=self.page_size).page()
        return ListCustomConnectorDocumentsResponseContent(documents=page.items, next_token=page.next_token)

    def _list_collections(
        self, request: ListCustomConnectorCollectionsRequest
    ) -> ListCustomConnectorCollectionsResponseContent:
        ordered = sorted(
            (
                collection
                for collection in self.collections.values()
                if request.collection_id is None or collection.collection_id == request.collection_id
            ),
            key=lambda collection: collection.collection_id,
        )
        page: _Page[Collection] = _Page[Collection](
            items=ordered, next_token=request.next_token, page_size=self.page_size
        ).page()
        return ListCustomConnectorCollectionsResponseContent(collections=page.items, next_token=page.next_token)

    def _delete_collections(self, request: BatchDeleteCustomConnectorCollectionsRequest) -> None:
        held = {collection_id for collection_id, _ in self.documents}
        if any(reference.collection_id in held for reference in request.collections):
            raise _Failures.conflict("The collection still holds documents")
        for reference in request.collections:
            self.collections.pop(reference.collection_id, None)

    def _put_checkpoint(self, request: PutCustomConnectorCheckpointRequest) -> None:
        write = _CollectionWrite(collection_id=request.collection_id, source_collection_id=request.source_collection_id)
        collection_id = write.checked_collection_id()
        collection = self._ensure_collection(write)
        self.collections[collection_id] = collection.model_copy(
            update={"checkpoint_data": request.checkpoint_data, "updated_at": _FAKE_TIME}
        )

    def _get_checkpoint(
        self, request: GetCustomConnectorCheckpointRequest
    ) -> GetCustomConnectorCheckpointResponseContent:
        collection = self.collections.get(request.collection_id)
        if collection is None or collection.checkpoint_data is None:
            return GetCustomConnectorCheckpointResponseContent()
        return GetCustomConnectorCheckpointResponseContent(
            checkpoint=Checkpoint(
                checkpoint_data=collection.checkpoint_data, created_at=_FAKE_TIME, updated_at=_FAKE_TIME
            )
        )

    def _inputs(self) -> JobInputs:
        if self.job_inputs is None:
            raise _Failures.not_found("The fake holds no job inputs")
        return self.job_inputs

    def _ensure_collection(self, write: _CollectionWrite) -> Collection:
        collection_id = write.collection_id
        return self.collections.setdefault(
            collection_id,
            Collection(
                collection_id=collection_id,
                source_collection_id=write.source_collection_id,
                created_at=_FAKE_TIME,
                updated_at=_FAKE_TIME,
            ),
        )
