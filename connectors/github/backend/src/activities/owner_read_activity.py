"""Read one owner's selected repositories into the store.

Each repository is one collection, named by its numeric ID. The stored collections and their
checkpoints come from one listing. A repository whose checkpoint matches its last push and the
content filters isn't downloaded, unless a full read is due. Collections of repositories no
longer selected are deleted. The default collection's checkpoint records the last full read.
"""

import logging
from datetime import timedelta

from custom_connector_framework import DeleteCollectionRequest, DocumentStore, SaveCheckpointRequest
from pydantic import BaseModel, ConfigDict

from common.clock import Clock
from common.configuration import GitHubConnectorConfiguration
from common.models import RepositoryCheckpoint, SourceCheckpoint
from common.reading.repository_reader import ReadRepositoryRequest, RepositoryOutcome, RepositoryReader
from common.reading.repository_selector import RepositorySelector

logger = logging.getLogger(__name__)


class ReadOwnerResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_read: bool
    repositories_read: int
    repositories_unchanged: int
    collections_deleted: int


class OwnerReadActivity:
    """Reads the owner's changed repositories and removes the collections of the ones that left the selection."""

    def __init__(
        self, selector: RepositorySelector, repository_reader: RepositoryReader, store: DocumentStore, clock: Clock
    ) -> None:
        self._selector = selector
        self._repository_reader = repository_reader
        self._store = store
        self._clock = clock

    def read(self, configuration: GitHubConnectorConfiguration) -> ReadOwnerResponse:
        started_at = self._clock.now()
        stored = {collection.collection_id: collection for collection in self._store.collections()}
        default = stored.get(None)
        source = None if default is None else default.parse_checkpoint(SourceCheckpoint)
        full_read = source is None or started_at - source.last_full_read_at >= timedelta(
            days=configuration.full_read_interval_days
        )
        outcomes = {
            repository.collection_id: self._repository_reader.read(
                ReadRepositoryRequest(
                    configuration=configuration,
                    repository=repository,
                    full_read=full_read,
                    stored=None
                    if repository.collection_id not in stored
                    else stored[repository.collection_id].parse_checkpoint(RepositoryCheckpoint),
                )
            )
            for repository in self._selector.select(configuration).repositories
        }
        kept = {
            collection_id for collection_id, outcome in outcomes.items() if outcome is not RepositoryOutcome.REMOVED
        }
        read = sum(1 for outcome in outcomes.values() if outcome is RepositoryOutcome.READ)
        unselected = [
            collection_id for collection_id in stored if collection_id is not None and collection_id not in kept
        ]
        for collection_id in unselected:
            self._store.delete_collection(DeleteCollectionRequest(collection_id=collection_id))
            logger.info("repository_collection_deleted", extra={"collection_id": collection_id})
        if full_read:
            self._store.save_checkpoint(
                SaveCheckpointRequest(checkpoint=SourceCheckpoint(last_full_read_at=started_at).model_dump_json())
            )
        response = ReadOwnerResponse(
            full_read=full_read,
            repositories_read=read,
            repositories_unchanged=len(kept) - read,
            collections_deleted=len(unselected),
        )
        logger.info("owner_read_completed", extra={"owner": configuration.owner, **response.model_dump()})
        return response
