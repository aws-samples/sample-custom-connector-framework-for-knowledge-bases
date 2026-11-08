"""The start-of-job sweep of the custom connector's files.

A crash can leave keys that no record names: a file written before its record was sent, the
old pair of an interrupted rename, or a metadata file written without its file. The sweep
lists every key under the custom connector's prefix and every document record, then deletes
each key under a document prefix that has no record. Under a prefix with a record it keeps the
most recently modified file and that file's metadata file and deletes the rest. It logs counts
and document IDs, never keys or file names; a key S3 refuses to delete is counted as failed,
with S3's error codes, and logged at WARNING.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from botocore.exceptions import BotoCoreError, ClientError
from ccf_api_models.identifiers import METADATA_SUFFIX, ConnectorRef, DocumentKey
from ccf_api_models.models import ListCustomConnectorDocumentsRequest, ListCustomConnectorDocumentsResponseContent
from pydantic import BaseModel, ConfigDict

from custom_connector_framework.client import CcfApiError
from custom_connector_framework.client.pages import Pages, PagesRequest
from custom_connector_framework.runner.aws_error_code import AwsErrorCode
from custom_connector_framework.runner.job_inputs_reader import FrameworkApiUnavailableError
from custom_connector_framework.runner.source_access import SourceAccessDeniedError
from custom_connector_framework.store.destination_objects import DestinationObjects, ListedKey

logger = logging.getLogger(__name__)


class SweepRequest(BaseModel):
    """The custom connector to sweep and its destination bucket, through the source-access session."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    connector: ConnectorRef
    objects: DestinationObjects


class _StoredPrefix(BaseModel):
    """One document prefix's keys, or one key outside the layout on its own."""

    model_config = ConfigDict(frozen=True)

    prefix: str
    document_id: str | None
    keys: tuple[ListedKey, ...]

    def extra_keys(self) -> list[str]:
        """Every key but the newest file and its metadata file."""
        files = [stored for stored in self.keys if not stored.key.endswith(METADATA_SUFFIX)]
        if not files:
            return [stored.key for stored in self.keys]
        newest = max(files, key=lambda stored: stored.last_modified).key
        kept = {newest, f"{newest}{METADATA_SUFFIX}"}
        return [stored.key for stored in self.keys if stored.key not in kept]


class FileSweeper:
    """Deletes the keys that no document record keeps."""

    def __init__(self, pages: Pages) -> None:
        self._pages = pages

    def sweep(self, request: SweepRequest) -> None:
        """Sweep, or fail the job start: 72 when S3 denies the source-access role, 76 when the framework API fails."""
        try:
            self._sweep(request)
        except (CcfApiError, BotoCoreError) as error:
            raise FrameworkApiUnavailableError("The sweep couldn't list the custom connector's records") from error
        except ClientError as error:
            if AwsErrorCode.denies(error):
                raise SourceAccessDeniedError("S3 denied the sweep of the custom connector's files") from error
            raise

    def _sweep(self, request: SweepRequest) -> None:
        recorded = self._recorded_prefixes(request.connector)
        deleted_total = failed_total = 0
        for stored in self._stored_prefixes(request):
            is_recorded = stored.prefix in recorded
            doomed = stored.extra_keys() if is_recorded else [listed.key for listed in stored.keys]
            if not doomed:
                continue
            failure = request.objects.delete(doomed)
            deleted = len(doomed) - failure.count
            deleted_total += deleted
            failed_total += failure.count
            logger.info(
                "sweep_deleted_keys",
                extra={"document_id": stored.document_id, "recorded": is_recorded, "deleted_key_count": deleted},
            )
            if failure.count > 0:
                logger.warning(
                    "sweep_delete_failed",
                    extra={
                        "document_id": stored.document_id,
                        "failed_key_count": failure.count,
                        "error_codes": list(failure.codes),
                    },
                )
        logger.info("sweep_finished", extra={"deleted_key_count": deleted_total, "failed_key_count": failed_total})

    def _recorded_prefixes(self, connector: ConnectorRef) -> set[str]:
        listing = ListCustomConnectorDocumentsRequest(
            destination_id=connector.destination_id, connector_id=connector.connector_id
        )
        pages = self._pages.of(
            PagesRequest[ListCustomConnectorDocumentsResponseContent](
                request=listing, response_type=ListCustomConnectorDocumentsResponseContent
            )
        )
        return {
            DocumentKey(
                connector_id=connector.connector_id, collection_id=stored.collection_id, document_id=stored.document_id
            ).prefix
            for page in pages
            for stored in page.documents
        }

    @staticmethod
    def _stored_prefixes(request: SweepRequest) -> list[_StoredPrefix]:
        grouped: defaultdict[str, list[ListedKey]] = defaultdict(list)
        document_ids: dict[str, str] = {}
        for listed in request.objects.list_keys(request.connector.key_prefix):
            document = DocumentKey.of_object_key(listed.key)
            prefix = listed.key if document is None else document.prefix
            if document is not None:
                document_ids[prefix] = document.document_id
            grouped[prefix].append(listed)
        return [
            _StoredPrefix(prefix=prefix, document_id=document_ids.get(prefix), keys=tuple(keys))
            for prefix, keys in grouped.items()
        ]
