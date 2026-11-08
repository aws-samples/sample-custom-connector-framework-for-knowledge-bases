"""The metadata attribute names every GitHub document carries.

The open-source MCP's `search` filters on `owner`, `repository`, and `repository_id`, and its
`fetch` returns them, so a change to these names must change the MCP too. The creation date is a
number, epoch seconds in UTC, because knowledge base range filters compare numbers.
"""

from enum import Enum


class DocumentAttributeName(str, Enum):
    OWNER = "owner"
    REPOSITORY = "repository"
    REPOSITORY_ID = "repository_id"
    PATH = "path"
    REPOSITORY_CREATED_AT = "repo_created_at"
