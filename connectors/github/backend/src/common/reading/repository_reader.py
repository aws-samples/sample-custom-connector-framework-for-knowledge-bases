"""Read one repository into its collection, unless its checkpoint shows it unchanged.

A repository that is read has its archive streamed once: every kept file is put, every stored
document the archive no longer holds is deleted, and the collection checkpoint is saved after
its documents.
"""

import logging
from enum import StrEnum

from custom_connector_framework import (
    DeleteDocumentRequest,
    DiffRequest,
    DocumentStore,
    SaveCheckpointRequest,
    SourceDocument,
)
from pydantic import BaseModel, ConfigDict

from common.configuration import GitHubConnectorConfiguration
from common.dao.github_dao import GitHubDao
from common.exceptions import ArchiveDownloadBrokenError, RepositoryNotFoundError
from common.models import GitHubRepository, RepositoryCheckpoint
from common.reading.archive_reader import ArchiveReader, ReadArchiveRequest
from common.reading.document_builder import BuildDocumentRequest, RepositoryDocumentBuilder
from common.sleeper import Sleeper

logger = logging.getLogger(__name__)


class RepositoryOutcome(StrEnum):
    READ = "read"
    UNCHANGED = "unchanged"
    REMOVED = "removed"
    """GitHub no longer has the repository, so its collection goes with the unselected ones."""


class ReadRepositoryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    configuration: GitHubConnectorConfiguration
    repository: GitHubRepository
    full_read: bool
    stored: RepositoryCheckpoint | None
    """The collection's checkpoint from the job's one collections listing; None when none parses."""


class RepositoryReader:
    def __init__(
        self,
        github: GitHubDao,
        archive_reader: ArchiveReader,
        document_builder: RepositoryDocumentBuilder,
        store: DocumentStore,
        sleeper: Sleeper,
    ) -> None:
        self._github = github
        self._archive_reader = archive_reader
        self._document_builder = document_builder
        self._store = store
        self._sleeper = sleeper

    def read(self, request: ReadRepositoryRequest) -> RepositoryOutcome:
        repository = request.repository
        current = RepositoryCheckpoint(
            pushed_at=repository.pushed_at, filters=request.configuration.content_filters_hash()
        )
        if not request.full_read and request.stored == current:
            logger.info("repository_skipped", extra={"repository": repository.full_name, "reason": "unchanged"})
            return RepositoryOutcome.UNCHANGED
        try:
            document_ids = self._put_with_retries(request)
        except RepositoryNotFoundError:
            logger.warning("repository_dropped", extra={"repository": repository.full_name, "reason": "not_found"})
            return RepositoryOutcome.REMOVED
        collection_id = repository.collection_id
        missing = self._store.diff(
            DiffRequest(
                current=tuple(SourceDocument(document_id=document_id) for document_id in document_ids),
                collection_id=collection_id,
            )
        ).missing
        for document_id in missing:
            self._store.delete(DeleteDocumentRequest(document_id=document_id, collection_id=collection_id))
        self._store.save_checkpoint(
            SaveCheckpointRequest(checkpoint=current.model_dump_json(), collection_id=collection_id)
        )
        logger.info(
            "repository_read",
            extra={"repository": repository.full_name, "documents": len(document_ids), "deleted": len(missing)},
        )
        return RepositoryOutcome.READ

    def _put_with_retries(self, request: ReadRepositoryRequest) -> tuple[str, ...]:
        """Read the archive again from the start when the download breaks, as often as the configuration allows.

        Files the broken attempt already put are put again; the store keeps one record per document.
        """
        access = request.configuration.github
        attempt = 0
        while True:
            try:
                return self._put_files(request)
            except ArchiveDownloadBrokenError:
                if attempt == access.retry_attempts:
                    raise
                attempt += 1
                logger.warning(
                    "archive_download_retried",
                    extra={"repository": request.repository.full_name, "attempt": attempt},
                )
                self._sleeper.sleep_up_to(access.retry_backoff_seconds * 2 ** (attempt - 1))

    def _put_files(self, request: ReadRepositoryRequest) -> tuple[str, ...]:
        """Put every kept file in one pass over the archive, and return their document IDs."""
        repository = request.repository
        document_ids: list[str] = []
        with self._github.open_archive(repository) as archive:
            files = self._archive_reader.read(
                ReadArchiveRequest(archive=archive, max_file_bytes=request.configuration.max_file_bytes)
            )
            for file in files:
                document = self._document_builder.build(BuildDocumentRequest(repository=repository, file=file))
                self._store.put(document)
                document_ids.append(document.document_id)
        return tuple(document_ids)
