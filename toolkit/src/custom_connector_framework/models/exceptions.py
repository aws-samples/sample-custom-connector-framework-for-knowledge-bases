"""Exceptions raised by the toolkit's document and sink handling."""


class DocumentError(Exception):
    """Base class for document-related errors."""


class UnsupportedDocumentError(DocumentError):
    """A document's file type could not be determined or is not supported."""


class SinkError(Exception):
    """Base class for errors writing to or deleting from a document sink."""


class SinkUploadError(SinkError):
    """A document or metadata sidecar could not be written to the object store."""


class SinkDeleteError(SinkError):
    """A document or metadata sidecar could not be deleted from the object store."""


class UnsupportedAclError(Exception):
    """An access control entry cannot be represented in the target provider's format."""
