"""The store's public models."""

from custom_connector_framework.store.models import RESERVED_ATTRIBUTE_NAMES, SkippedCounts, SkipReason


def test_skipped_counts_has_one_member_per_skip_reason() -> None:
    assert set(SkippedCounts.model_fields) == {reason.value for reason in SkipReason}


def test_the_reserved_attribute_names_are_the_wire_models_own_members() -> None:
    assert RESERVED_ATTRIBUTE_NAMES == {"document_arn", "title", "source_uri", "_source_uri"}
