"""Document access control lists, which the store writes into each destination's metadata file format.

Amazon Bedrock knowledge bases and Amazon Quick both let a metadata file say who may see a
document, each in its own JSON format. A connector implementation builds one `AccessControl`
for a document, and the store renders it for the destination: Bedrock's `accessControlList`
for a `BEDROCK` destination with access control lists turned on, Quick's
`AccessControlList` for `QUICK`, and nothing for `S3`.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict


class UnsupportedAccessControlError(ValueError):
    """The destination can't represent an entry of the document's access control list."""


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

    `principal_id` is the user's email for `PrincipalType.USER` (both services match users
    by exact email) or the source system's group identifier for `PrincipalType.GROUP`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str
    principal_type: PrincipalType
    access: AccessEffect


class AccessControl(BaseModel):
    """The access control list for one document.

    An empty `entries` list means the connector implementation did not set access control on the
    document. The destination's metadata format decides what that means.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[AccessControlEntry, ...] = ()

    def is_empty(self) -> bool:
        return len(self.entries) == 0
