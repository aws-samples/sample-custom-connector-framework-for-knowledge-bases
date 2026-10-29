"""GitLab full-snapshot connector definition."""

import hashlib
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any, cast
from urllib.parse import quote

import gitlab
from custom_connector_framework import (
    ConnectorContext,
    ConnectorDefinition,
    Document,
    DocumentMetadata,
    EmptyJobConfiguration,
)
from custom_connector_framework._safe_http import SourceEndpoint
from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_EXTENSIONS = (
    ".c",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".json",
    ".kt",
    ".md",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
)


class GitLabProjectConfiguration(BaseModel):
    """One project included in the complete source snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    project_id: str = Field(..., min_length=1)
    ref: str = Field(default="main", min_length=1)
    file_extensions: tuple[str, ...] = _DEFAULT_EXTENSIONS


class GitLabConnectorConfiguration(BaseModel):
    """Source settings fetched by the toolkit for every GitLab job."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    token_secret_arn: str = Field(..., min_length=1)
    projects: tuple[GitLabProjectConfiguration, ...] = Field(..., min_length=1)
    server_url: SourceEndpoint = "https://gitlab.com"


def _secret_value(source_session: Any, secret_arn: str) -> str:
    response = cast(dict[str, Any], source_session.client("secretsmanager").get_secret_value(SecretId=secret_arn))
    token = response.get("SecretString")
    if not isinstance(token, str) or not token:
        raise RuntimeError("The configured GitLab credential is unavailable.")
    return token


def _document_id(project_id: str, path: str) -> str:
    identity = f"gitlab\0{project_id}\0{path}".encode("utf-8")  # noqa: UP012 -- Identity encoding is explicit.
    return f"gitlab_{hashlib.sha256(identity).hexdigest()}"


def _category_document_id(project_id: str, category: str, iid: int) -> str:
    identity = f"gitlab\0{project_id}\0{category}\0{iid}".encode("utf-8")  # noqa: UP012
    return f"gitlab_{hashlib.sha256(identity).hexdigest()}"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _author(entry: Any) -> str:
    author = getattr(entry, "author", None)
    return _text(author.get("username")) if isinstance(author, dict) else ""


def _markdown(title: str, iid: int, state: str, author: str, created_at: str, updated_at: str, body: str) -> bytes:
    return (  # noqa: UP012 -- Markdown encoding is explicit.
        f"# {title}\n\n"
        f"- IID: {iid}\n"
        f"- State: {state}\n"
        f"- Author: {author}\n"
        f"- Created: {created_at}\n"
        f"- Updated: {updated_at}\n\n"
        f"{body}\n"
    ).encode("utf-8")


def _category_document(project: Any, entry: Any, category: str, filename_label: str) -> Document:
    iid = cast(int, entry.iid)
    title = _text(entry.title)
    state = _text(entry.state)
    created_at = _text(entry.created_at)
    updated_at = _text(entry.updated_at)
    return Document.from_bytes(
        document_id=_category_document_id(str(project.id), category, iid),
        content=_markdown(
            title, iid, state, _author(entry), created_at, updated_at, _text(getattr(entry, "description", None))
        ),
        filename=f"{filename_label}-{iid}.md",
        metadata=DocumentMetadata(
            title=title,
            source_uri=_text(entry.web_url),
            created_at=created_at,
            last_updated_at=updated_at,
            attributes={"provider": "gitlab", "category": category, "state": state},
        ),
    )


def _project_documents(project: Any, config: GitLabProjectConfiguration) -> Iterator[Document]:
    extensions = tuple(extension.lower() for extension in config.file_extensions)
    entries = project.repository_tree(ref=config.ref, recursive=True, iterator=True, per_page=100)
    for entry in entries:
        if entry["type"] != "blob":
            continue
        path = cast(str, entry["path"])
        if not path.lower().endswith(extensions):
            continue
        file_content = project.files.get(file_path=path, ref=config.ref).decode()
        yield Document.from_bytes(
            document_id=_document_id(str(project.id), path),
            content=cast(bytes, file_content),
            filename=PurePosixPath(path).name,
            metadata=DocumentMetadata(
                title=path,
                source_uri=f"{project.web_url}/-/blob/{quote(config.ref, safe='')}/{quote(path, safe='/')}",
                attributes={"provider": "gitlab", "project": config.project_id, "ref": config.ref},
            ),
        )


def documents(context: ConnectorContext[GitLabConnectorConfiguration, EmptyJobConfiguration]) -> Iterator[Document]:
    """Yield files, issues, and merge requests from every configured project."""
    config = context.connector_configuration
    token = _secret_value(context.source_session, config.token_secret_arn)
    client = gitlab.Gitlab(url=config.server_url, private_token=token)
    for project_config in config.projects:
        project = client.projects.get(project_config.project_id)
        yield from _project_documents(project, project_config)
        for issue in project.issues.list(state="all", iterator=True, per_page=100):
            yield _category_document(project, issue, "issue", "issue")
        for merge_request in project.mergerequests.list(state="all", iterator=True, per_page=100):
            yield _category_document(project, merge_request, "merge_request", "merge-request")


definition = ConnectorDefinition(
    connector_configuration_model=GitLabConnectorConfiguration,
    job_configuration_model=EmptyJobConfiguration,
    documents=documents,
)
