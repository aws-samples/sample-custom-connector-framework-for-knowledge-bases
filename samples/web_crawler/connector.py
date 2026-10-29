"""Public web crawler full-snapshot connector definition."""

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from custom_connector_framework import (
    AccessControl,
    AccessControlEntry,
    AccessEffect,
    ConnectorContext,
    ConnectorDefinition,
    Document,
    DocumentMetadata,
    EmptyJobConfiguration,
    PrincipalType,
)
from custom_connector_framework._safe_http import HttpsUrl, fetch_public_https
from pydantic import BaseModel, ConfigDict, Field

_PDF_CONTENT_TYPE = "application/pdf"


class CrawlAclEntry(BaseModel):
    """One principal entry in a URL ACL."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    principal_id: str = Field(..., min_length=1)
    principal_type: PrincipalType = PrincipalType.USER
    access: AccessEffect = AccessEffect.ALLOW


class CrawlUrl(BaseModel):
    """One URL and an optional ACL override."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    url: HttpsUrl
    acl: tuple[CrawlAclEntry, ...] = ()


class CrawlerConnectorConfiguration(BaseModel):
    """Crawler settings fetched by the toolkit."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    urls: tuple[CrawlUrl, ...] = Field(..., min_length=1)
    default_acl: tuple[CrawlAclEntry, ...] = ()


@dataclass(frozen=True)
class CrawledPage:
    """Fully fetched page content prepared for document creation."""

    content: bytes
    title: str
    suffix: str


def _document_id(url: str) -> str:
    return f"web_{hashlib.sha256(url.encode()).hexdigest()}"


def _fetch(url: str) -> CrawledPage:
    response = fetch_public_https(url)
    if _PDF_CONTENT_TYPE in response.content_type:
        title = urlparse(response.final_url).path.rsplit("/", 1)[-1] or "document.pdf"
        return CrawledPage(content=response.content, title=title, suffix=".pdf")
    soup = BeautifulSoup(response.content, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else "Untitled page"
    return CrawledPage(content=response.content, title=title, suffix=".html")


def _access_control(entries: tuple[CrawlAclEntry, ...]) -> AccessControl:
    return AccessControl(
        entries=tuple(
            AccessControlEntry(
                principal_id=entry.principal_id,
                principal_type=entry.principal_type,
                access=entry.access,
            )
            for entry in entries
        )
    )


def documents(context: ConnectorContext[CrawlerConnectorConfiguration, EmptyJobConfiguration]) -> Iterator[Document]:
    """Yield every configured page in one complete snapshot."""
    config = context.connector_configuration
    for crawl_url in config.urls:
        page = _fetch(crawl_url.url)
        entries = crawl_url.acl or config.default_acl
        yield Document.from_bytes(
            document_id=_document_id(crawl_url.url),
            content=page.content,
            filename=f"page{page.suffix}",
            metadata=DocumentMetadata(
                title=page.title,
                source_uri=crawl_url.url,
                attributes={"domain": urlparse(crawl_url.url).netloc},
                access_control=_access_control(entries),
            ),
        )


definition = ConnectorDefinition(
    connector_configuration_model=CrawlerConnectorConfiguration,
    job_configuration_model=EmptyJobConfiguration,
    documents=documents,
)
