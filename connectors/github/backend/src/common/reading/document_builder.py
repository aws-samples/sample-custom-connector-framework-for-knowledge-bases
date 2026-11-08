"""Turn one archive file into a document for the store.

Each document's ID is `<numeric repository ID>/<path>`, it belongs to its repository's
collection, and its version is the file's git blob SHA. It carries the source fields search
filters on as attributes, a title, and the file's GitHub URL. No attribute changes with a push,
so a push rewrites only the files it changed.
"""

import io
from pathlib import PurePosixPath
from urllib.parse import quote

from custom_connector_framework import DocumentAttribute, NumberAttribute, PutDocumentRequest, StringAttribute
from pydantic import BaseModel, ConfigDict

from common.attributes import DocumentAttributeName
from common.models import GitHubRepository
from common.reading.archive_reader import ArchiveFile


class BuildDocumentRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository: GitHubRepository
    file: ArchiveFile


class RepositoryDocumentBuilder:
    """Builds the store request for one file of a repository."""

    def build(self, request: BuildDocumentRequest) -> PutDocumentRequest:
        repository = request.repository
        path = request.file.path
        return PutDocumentRequest(
            document_id=f"{repository.id}/{path}",
            collection_id=repository.collection_id,
            file_name=PurePosixPath(path).name,
            content=io.BytesIO(request.file.content),
            source_version=request.file.blob_sha,
            title=f"{repository.full_name}/{path}",
            source_uri=(
                f"{repository.html_url}/blob/{quote(repository.default_branch, safe='')}/{quote(path, safe='/')}"
            ),
            attributes=(
                DocumentAttribute(
                    name=DocumentAttributeName.OWNER.value, value=StringAttribute(value=repository.owner)
                ),
                DocumentAttribute(
                    name=DocumentAttributeName.REPOSITORY.value, value=StringAttribute(value=repository.full_name)
                ),
                DocumentAttribute(
                    name=DocumentAttributeName.REPOSITORY_ID.value,
                    value=StringAttribute(value=repository.repository_id_attribute),
                ),
                DocumentAttribute(name=DocumentAttributeName.PATH.value, value=StringAttribute(value=path)),
                DocumentAttribute(
                    name=DocumentAttributeName.REPOSITORY_CREATED_AT.value,
                    value=NumberAttribute(value=int(repository.created_at.timestamp())),
                ),
            ),
        )
