"""GitHub full-snapshot connector definition."""

import hashlib
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any, cast
from urllib.parse import quote

from custom_connector_framework import (
    ConnectorContext,
    ConnectorDefinition,
    Document,
    DocumentMetadata,
    EmptyJobConfiguration,
)
from custom_connector_framework._safe_http import SourceEndpoint
from github import Auth, Github
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


class GitHubRepositoryConfiguration(BaseModel):
    """One repository included in the complete source snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    full_name: str = Field(..., min_length=3)
    ref: str = Field(default="main", min_length=1)
    file_extensions: tuple[str, ...] = _DEFAULT_EXTENSIONS


class GitHubConnectorConfiguration(BaseModel):
    """Source settings fetched by the toolkit for every GitHub job."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    token_secret_arn: str = Field(..., min_length=1)
    repositories: tuple[GitHubRepositoryConfiguration, ...] = Field(..., min_length=1)
    api_url: SourceEndpoint = "https://api.github.com"


def _secret_value(source_session: Any, secret_arn: str) -> str:
    response = cast(dict[str, Any], source_session.client("secretsmanager").get_secret_value(SecretId=secret_arn))
    token = response.get("SecretString")
    if not isinstance(token, str) or not token:
        raise RuntimeError("The configured GitHub credential is unavailable.")
    return token


def _document_id(repository_id: str, path: str) -> str:
    identity = f"github\0{repository_id}\0{path}".encode("utf-8")  # noqa: UP012 -- Identity encoding is explicit.
    return f"github_{hashlib.sha256(identity).hexdigest()}"


def _category_document_id(repository_id: str, category: str, number: int) -> str:
    identity = f"github\0{repository_id}\0{category}\0{number}".encode("utf-8")  # noqa: UP012
    return f"github_{hashlib.sha256(identity).hexdigest()}"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _timestamp(value: Any) -> str:
    return "" if value is None else str(value.isoformat())


def _markdown(title: str, number: int, state: str, author: str, created_at: str, updated_at: str, body: str) -> bytes:
    return (  # noqa: UP012 -- Markdown encoding is explicit.
        f"# {title}\n\n"
        f"- Number: {number}\n"
        f"- State: {state}\n"
        f"- Author: {author}\n"
        f"- Created: {created_at}\n"
        f"- Updated: {updated_at}\n\n"
        f"{body}\n"
    ).encode("utf-8")


def _category_document(repository: Any, entry: Any, category: str, filename_label: str) -> Document:
    number = cast(int, entry.number)
    title = _text(entry.title)
    state = _text(entry.state)
    created_at = _timestamp(entry.created_at)
    updated_at = _timestamp(entry.updated_at)
    user = getattr(entry, "user", None)
    author = _text(getattr(user, "login", None))
    return Document.from_bytes(
        document_id=_category_document_id(str(repository.id), category, number),
        content=_markdown(title, number, state, author, created_at, updated_at, _text(getattr(entry, "body", None))),
        filename=f"{filename_label}-{number}.md",
        metadata=DocumentMetadata(
            title=title,
            source_uri=_text(entry.html_url),
            created_at=created_at,
            last_updated_at=updated_at,
            attributes={"provider": "github", "category": category, "state": state},
        ),
    )


def _repository_paths(repository: Any, ref: str) -> Iterator[str]:
    pending_trees = [(ref, "")]
    while pending_trees:
        tree_id, prefix = pending_trees.pop()
        tree = repository.get_git_tree(tree_id, recursive=False)
        if tree.truncated:
            raise RuntimeError("GitHub returned an incomplete repository tree.")
        child_trees: list[tuple[str, str]] = []
        for element in sorted(tree.tree, key=lambda entry: entry.path):
            path = f"{prefix}{element.path}"
            if element.type == "tree":
                child_trees.append((element.sha, f"{path}/"))
            elif element.type == "blob":
                yield path
        pending_trees.extend(reversed(child_trees))


def _repository_documents(repository: Any, config: GitHubRepositoryConfiguration) -> Iterator[Document]:
    extensions = tuple(extension.lower() for extension in config.file_extensions)
    for path in _repository_paths(repository, config.ref):
        if not path.lower().endswith(extensions):
            continue
        file_content = repository.get_contents(path, ref=config.ref)
        if isinstance(file_content, list):
            raise RuntimeError("GitHub returned a directory for a selected file.")
        yield Document.from_bytes(
            document_id=_document_id(str(repository.id), path),
            content=cast(bytes, file_content.decoded_content),
            filename=PurePosixPath(path).name,
            metadata=DocumentMetadata(
                title=path,
                source_uri=f"{repository.html_url}/blob/{quote(config.ref, safe='')}/{quote(path, safe='/')}",
                attributes={"provider": "github", "repository": config.full_name, "ref": config.ref},
            ),
        )


def documents(context: ConnectorContext[GitHubConnectorConfiguration, EmptyJobConfiguration]) -> Iterator[Document]:
    """Yield files, issues, and pull requests from every configured repository."""
    config = context.connector_configuration
    token = _secret_value(context.source_session, config.token_secret_arn)
    github = Github(base_url=config.api_url, auth=Auth.Token(token))
    for repository_config in config.repositories:
        repository = github.get_repo(repository_config.full_name)
        yield from _repository_documents(repository, repository_config)
        for issue in repository.get_issues(state="all"):
            if issue.raw_data.get("pull_request") is None:
                yield _category_document(repository, issue, "issue", "issue")
        for pull_request in repository.get_pulls(state="all"):
            yield _category_document(repository, pull_request, "pull_request", "pull-request")


definition = ConnectorDefinition(
    connector_configuration_model=GitHubConnectorConfiguration,
    job_configuration_model=EmptyJobConfiguration,
    documents=documents,
)
