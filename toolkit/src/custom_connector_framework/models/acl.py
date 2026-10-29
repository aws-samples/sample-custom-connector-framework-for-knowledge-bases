"""Provider-neutral document access control model.

Amazon Bedrock Managed Knowledge Bases and Amazon Quick Knowledge Bases both let
you say who may see a document, each in its own JSON format. A connector builds
one `AccessControl` for a document, and the provider adapter renders it into the
shape that provider expects.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict


class PrincipalType(str, Enum):
    """The kind of principal an access control entry names."""

    USER = "USER"
    GROUP = "GROUP"


class AccessEffect(str, Enum):
    """Whether a principal is granted or denied access. DENY always wins."""

    ALLOW = "ALLOW"
    DENY = "DENY"


class AccessControlEntry(BaseModel):
    """One principal's access to a document.

    `principal_id` is the user's email for `PrincipalType.USER` (both providers
    match users by exact email) or the source system's group identifier for
    `PrincipalType.GROUP`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str
    principal_type: PrincipalType
    access: AccessEffect


class AccessControl(BaseModel):
    """The access control list for one document.

    An empty `entries` list means the connector did not set access control on the
    document. A provider adapter decides what that means for its destination.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[AccessControlEntry, ...] = ()

    def is_empty(self) -> bool:
        """Return whether this document has no access control entries."""
        return len(self.entries) == 0
