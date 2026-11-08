"""Access control lists: the models the store renders."""

from custom_connector_framework.store.access_control import (
    AccessControl,
    AccessControlEntry,
    AccessEffect,
    PrincipalType,
)


def test_an_access_control_list_without_entries_is_empty() -> None:
    assert AccessControl().is_empty()
    entry = AccessControlEntry(
        principal_id="dev@example.com", principal_type=PrincipalType.USER, access=AccessEffect.ALLOW
    )
    assert not AccessControl(entries=(entry,)).is_empty()
