---
description: The Custom Connector Framework toolkit's document store, access control lists, and collection checkpoints.
---

# Store documents and checkpoints

Your implementation writes everything through `self.store`, the toolkit's `DocumentStore`. Every store request names documents and collections by your source's own IDs, and the store turns them into the framework's `doc-` and `col-` IDs. A `collection_id` of `None` means the default collection.

## Store methods

| Method | What it does |
| --- | --- |
| `put(PutDocumentRequest)` | Writes one document. Members: `document_id`, `file_name`, `content` (a binary stream), `attributes` (a tuple of `DocumentAttribute(name, value)`, each value a `StringAttribute`, `NumberAttribute`, or `StringListAttribute`, with unique names), `collection_id`, `source_version`, `media_type`, `access_control`, `title`, `source_uri`. |
| `delete(DeleteDocumentRequest)` | Queues a delete of `document_id` in `collection_id`. A document that isn't stored is ignored. |
| `documents(ListDocumentsRequest)` | Yields each stored document of a collection: `document_id`, `source_version`, `updated_at`. |
| `diff(DiffRequest)` | Compares `current`, the `SourceDocument`s (`document_id`, `source_version`) a collection has now, with what's stored, and returns the `new`, `changed`, and `missing` IDs. |
| `collections()` | Yields each stored collection: `collection_id` (`None` for the default) and `checkpoint`. |
| `get_checkpoint(GetCheckpointRequest)` | Returns a collection's checkpoint, or `None`. |
| `save_checkpoint(SaveCheckpointRequest)` | Saves a collection's `checkpoint`, at most 16 KB, after it sends that collection's queued puts and deletes. |
| `delete_collection(DeleteCollectionRequest)` | Deletes a collection and every document in it. |
| `get_report()` | Returns a `StoreReport`: written, unchanged, deleted, skipped, and pending counts per collection and in total. |

`put` reads the first 8 KB to detect the media type, unless you pass `media_type`. It skips empty text and content the destination can't index. When it skips a document the framework already holds, it queues a delete, so the old version doesn't stay searchable. Otherwise it writes the metadata file, then the file, and queues the record, which the store sends in batches of up to 99 per collection.

When the stored file already has the same content hash, `put` writes nothing and only refreshes the record. A job that finds no changes in the source rewrites no file, so the next ingestion has nothing new to index.

The attributes you write go into the document's metadata file. [Amazon Bedrock](https://aws.amazon.com/bedrock/) can filter a search on them. To declare attributes on a `BEDROCK` destination's knowledge base, see [metadata fields](destinations.md#declare-metadata-fields).

## Access control lists

Set `PutDocumentRequest.access_control` to an `AccessControl` with `AccessControlEntry` items: `principal_id`, `principal_type` (`USER` or `GROUP`), and `access` (`ALLOW` or `DENY`). A `USER` principal is the user's email address, which Amazon Bedrock and [AWS Quick](https://aws.amazon.com/quick/) match exactly. What the store does with the list depends on the destination:

| Destination | What the store does |
| --- | --- |
| `BEDROCK` with `access_control_enabled` `true` | Writes the list into the metadata file in the Amazon Bedrock format. A `GROUP` entry raises `UnsupportedAccessControlError`. Amazon Bedrock ingests no document without an access list, and a search without a user returns nothing. |
| `BEDROCK` without access control lists | Leaves the list out, and logs one WARNING per flush with the number of documents that had one. |
| `QUICK` | Writes the list whenever a document has one. You turn on access control lists when you connect AWS Quick to the bucket. |
| `S3` | Writes no list. |

## Checkpoints

A checkpoint is a string your implementation keeps for one collection between jobs, often JSON. The framework stores it encrypted and never parses it.

- `get_checkpoint` parses a checkpoint into the Pydantic model you name. It returns `None` when nothing is stored or the text doesn't parse.
- `collections()` lists every stored collection with its checkpoint, so one call reads them all.
- Saving a collection's checkpoint first sends that collection's queued writes, so a checkpoint never gets ahead of the documents it covers.

A job that fails partway keeps the collections it finished, and the next job resumes from their checkpoints. A connector can read a large source over several jobs this way, without starting over.

### Example: read only what changed

Consider a wiki where each space is a collection and the wiki can list the pages changed since a given time. Reading only changes is fast but never notices a deleted page, so the implementation returns to a full read every 7 days:

```python
from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, BaseModel

from custom_connector_framework import (
    ConnectorImplementation,
    DeleteDocumentRequest,
    DiffRequest,
    GetCheckpointRequest,
    SaveCheckpointRequest,
    SourceDocument,
)

FULL_READ_INTERVAL = timedelta(days=7)


class FullRead(BaseModel):
    last_full_read: AwareDatetime


class SpaceRead(BaseModel):
    changed_since: AwareDatetime


class WikiConnector(ConnectorImplementation):
    def run(self) -> None:
        now = datetime.now(UTC)
        last = self.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=FullRead))
        full_read = last is None or now - last.last_full_read > FULL_READ_INTERVAL
        wiki = connect_to_wiki(self.job)  # your source client
        for space in wiki.spaces():
            stored = self.store.get_checkpoint(GetCheckpointRequest(checkpoint_type=SpaceRead, collection_id=space.key))
            since = None if full_read or stored is None else stored.changed_since
            pages = wiki.pages(space.key, changed_since=since)
            for page in pages:
                self.store.put(page.to_put_request(collection_id=space.key))
            if since is None:
                current = tuple(SourceDocument(document_id=page.id) for page in pages)
                for page_id in self.store.diff(DiffRequest(current=current, collection_id=space.key)).missing:
                    self.store.delete(DeleteDocumentRequest(document_id=page_id, collection_id=space.key))
            checkpoint = SpaceRead(changed_since=now).model_dump_json()
            self.store.save_checkpoint(SaveCheckpointRequest(checkpoint=checkpoint, collection_id=space.key))
        if full_read:
            self.store.save_checkpoint(SaveCheckpointRequest(checkpoint=FullRead(last_full_read=now).model_dump_json()))
```

`connect_to_wiki`, `spaces`, `pages`, and `to_put_request` stand for your source's own client. A space that no longer exists needs `delete_collection`, which the example GitHub connector does for repositories that drop out of its listing.

To make a collection start over, call `DeleteCustomConnectorCheckpoint` between jobs. The next job gets no checkpoint for that collection.
