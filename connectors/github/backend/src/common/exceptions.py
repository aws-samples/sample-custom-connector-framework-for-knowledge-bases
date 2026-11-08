"""Failures that remove one repository from the read, or that fail the job."""


class RepositoryNotFoundError(RuntimeError):
    """GitHub has no archive for a repository the listing returned. Its collection is deleted."""


class ArchiveDownloadBrokenError(RuntimeError):
    """GitHub's archive download broke off before the archive ended. The repository is read again."""


class ArchiveDownloadRefusedError(RuntimeError):
    """GitHub answered an archive download with a status other than 200 or 404. The job fails."""


class MissingConnectorInputError(RuntimeError):
    """The custom connector has no configuration or no secret, so the job can't read GitHub."""
