"""What one job's store did, counted per source collection, and the report built from the counts."""

from __future__ import annotations

from collections import Counter
from typing import Protocol

from ccf_api_models.base import ValueModel
from pydantic import ConfigDict, Field

from custom_connector_framework.store.models import (
    CollectionCounts,
    CollectionReport,
    ReportCounts,
    SkippedCounts,
    SkipReason,
    StoreReport,
)


class PendingRecords(Protocol):
    def count_pending(self, source_collection_id: str | None) -> int: ...


class SentRecords(ValueModel):
    """What one send recorded for one collection."""

    source_collection_id: str | None
    puts: int
    deletes: int


class SkippedDocument(ValueModel):
    source_collection_id: str | None
    reason: SkipReason


class _Counts(ValueModel):
    model_config = ConfigDict(frozen=False)

    written: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: Counter[SkipReason] = Field(default_factory=Counter)

    def counts(self, pending: int) -> ReportCounts:
        return ReportCounts(
            written=self.written,
            unchanged=self.unchanged,
            deleted=self.deleted,
            skipped=SkippedCounts(**{reason.value: self.skipped[reason] for reason in SkipReason}),
            pending=pending,
        )


class StoreCounters:
    """Counts written, unchanged, skipped, and deleted documents, and which collections were written or read."""

    def __init__(self) -> None:
        self._counts: dict[str | None, _Counts] = {}
        self._written: set[str | None] = set()
        self._read: set[str | None] = set()
        self._deleted: set[str | None] = set()
        self._access_control_left_out = 0

    def queued(self, source_collection_id: str | None) -> None:
        """A record was queued, so the collection's pending records are reported."""
        self._of(source_collection_id)

    def written(self, source_collection_id: str | None) -> None:
        self._of(source_collection_id).written += 1

    def unchanged(self, source_collection_id: str | None) -> None:
        self._of(source_collection_id).unchanged += 1

    def skipped(self, document: SkippedDocument) -> None:
        self._of(document.source_collection_id).skipped[document.reason] += 1

    def sent(self, records: SentRecords) -> None:
        self._of(records.source_collection_id).deleted += records.deletes
        if records.puts or records.deletes:
            self._written.add(records.source_collection_id)

    def checkpoint_saved(self, source_collection_id: str | None) -> None:
        self._written.add(source_collection_id)

    def checkpoint_read(self, source_collection_id: str | None) -> None:
        self._read.add(source_collection_id)

    def collection_deleted(self, source_collection_id: str | None) -> None:
        self._deleted.add(source_collection_id)

    def access_control_left_out(self) -> None:
        self._access_control_left_out += 1

    def take_access_control_left_out(self) -> int:
        """How many documents left out their access control list since the last call; counting starts again."""
        left_out, self._access_control_left_out = self._access_control_left_out, 0
        return left_out

    def report(self, pending: PendingRecords) -> StoreReport:
        """Per-collection and total counts; changes nothing."""
        collections = tuple(
            CollectionReport(collection_id=collection, counts=counts.counts(pending.count_pending(collection)))
            for collection, counts in self._counts.items()
        )
        reports = [collection.counts for collection in collections]
        total = _Counts(
            written=sum(report.written for report in reports),
            unchanged=sum(report.unchanged for report in reports),
            deleted=sum(report.deleted for report in reports),
            skipped=sum((counts.skipped for counts in self._counts.values()), Counter()),
        ).counts(sum(report.pending for report in reports))
        written = self._written - self._deleted
        return StoreReport(
            collections=collections,
            total=total,
            collection_counts=CollectionCounts(
                written=len(written),
                unchanged=len(self._read - written - self._deleted),
                deleted=len(self._deleted),
            ),
        )

    def _of(self, source_collection_id: str | None) -> _Counts:
        return self._counts.setdefault(source_collection_id, _Counts())
