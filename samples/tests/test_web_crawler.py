"""Focused source mapping and outbound-policy tests for the crawler."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from custom_connector_framework import AccessEffect, ConnectorContext, EmptyJobConfiguration, PrincipalType
from custom_connector_framework import _safe_http as safe_http
from custom_connector_framework.providers.bedrock import BedrockKnowledgeBaseAdapter
from custom_connector_framework.providers.s3 import S3Adapter
from pydantic import TypeAdapter, ValidationError

from web_crawler import connector
from web_crawler.connector import CrawlAclEntry, CrawlerConnectorConfiguration, CrawlUrl

_PUBLIC_ADDRESS = "93.184.216.34"
_MALFORMED_HOST_URLS = (
    "https://_bad/path",
    "https://-bad.example/path",
    "https://bad-.example/path",
    "https://bad..example/path",
    "https://./path",
    f"https://{'a' * 64}.example/path",
    f"https://{'a.' * 127}example/path",
)
_VALID_ENTERPRISE_URLS = (
    "https://intranet/base/path",
    "https://123/base/path",
    "https://bücher.example/base/path",
    "https://example.com./base/path",
    "https://192.0.2.1:8443/base/path",
    "https://[2001:4860:4860::8888]:8443/base/path",
)


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False

    def stream(self, amount: int, *, decode_content: bool) -> Iterator[bytes]:
        assert amount == 64 * 1024
        assert decode_content
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class FakePool:
    def __init__(self, host: str, responses: list[FakeResponse], **configuration: Any) -> None:
        self.host = host
        self.configuration = configuration
        self._responses = responses
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def urlopen(self, method: str, target: str, **request: Any) -> FakeResponse:
        self.requests.append((method, target, request))
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    addresses_by_host: dict[str, tuple[str, ...]],
    responses: list[FakeResponse],
) -> list[FakePool]:
    pools: list[FakePool] = []

    def getaddrinfo(host: str, port: int, *, type: int) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert type == safe_http.socket.SOCK_STREAM
        return [
            (
                safe_http.socket.AF_INET6 if ":" in address else safe_http.socket.AF_INET,
                type,
                6,
                "",
                (address, port),
            )
            for address in addresses_by_host.get(host, ())
        ]

    def create_pool(host: str, **configuration: Any) -> FakePool:
        pool = FakePool(host, responses, **configuration)
        pools.append(pool)
        return pool

    monkeypatch.setattr(safe_http.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(safe_http.urllib3, "HTTPSConnectionPool", create_pool)
    return pools


def crawler_context(config: CrawlerConnectorConfiguration) -> Any:
    return ConnectorContext(
        connector_configuration=config,
        job_configuration=EmptyJobConfiguration(),
        source_session=object(),
    )


def test_configuration_is_strict_frozen_and_uses_shared_empty_job_model() -> None:
    config = CrawlerConnectorConfiguration(urls=(CrawlUrl(url="https://example.com/path?query=value"),))

    assert connector.definition.job_configuration_model is EmptyJobConfiguration
    with pytest.raises(ValidationError):
        CrawlerConnectorConfiguration.model_validate({"urls": [], "unexpected": True})
    with pytest.raises(ValidationError):
        config.urls = ()  # type: ignore[misc]


@pytest.mark.parametrize("url", ("http://example.com", "https://user@example.com"))
def test_configuration_rejects_non_https_or_userinfo_url(url: str) -> None:
    with pytest.raises(ValidationError):
        CrawlUrl(url=url)


@pytest.mark.parametrize("url", _MALFORMED_HOST_URLS)
def test_source_endpoint_rejects_malformed_hostname(url: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(safe_http.SourceEndpoint).validate_python(url)


@pytest.mark.parametrize("url", _MALFORMED_HOST_URLS)
def test_crawler_configuration_rejects_malformed_hostname(url: str) -> None:
    with pytest.raises(ValidationError):
        CrawlUrl(url=url)


@pytest.mark.parametrize("url", _VALID_ENTERPRISE_URLS)
def test_source_endpoint_and_crawler_accept_supported_hostname_forms(url: str) -> None:
    assert TypeAdapter(safe_http.SourceEndpoint).validate_python(url) == url
    assert CrawlUrl(url=url).url == url


def test_documents_are_lazy_and_map_configured_html_pdf_and_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        "https://example.com/page": safe_http.PublicHttpResponse(
            content=b"<html><head><title>Hello</title></head><body>hi</body></html>",
            content_type="text/html",
            final_url="https://example.com/page",
        ),
        "https://example.com/document.pdf": safe_http.PublicHttpResponse(
            content=b"%PDF-1.4 fake",
            content_type="application/pdf",
            final_url="https://example.com/document.pdf",
        ),
    }
    calls: list[str] = []

    def fetch(url: str) -> safe_http.PublicHttpResponse:
        calls.append(url)
        return responses[url]

    monkeypatch.setattr(connector, "fetch_public_https", fetch)
    config = CrawlerConnectorConfiguration(
        urls=(
            CrawlUrl(
                url="https://example.com/page",
                acl=(CrawlAclEntry(principal_id="person@example.com", access=AccessEffect.ALLOW),),
            ),
            CrawlUrl(url="https://example.com/document.pdf"),
        )
    )

    generated = connector.documents(crawler_context(config))
    assert calls == []
    snapshot = list(generated)

    assert calls == list(responses)
    assert [document.metadata.title for document in snapshot] == ["Hello", "document.pdf"]
    assert [document.suffix for document in snapshot] == [".html", ".pdf"]
    assert snapshot[0].metadata.access_control.entries[0].principal_type is PrincipalType.USER
    assert all(document.id.startswith("web_") for document in snapshot)


def test_fetch_failure_propagates_during_snapshot_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CrawlerConnectorConfiguration(urls=(CrawlUrl(url="https://example.com/fail"),))
    monkeypatch.setattr(
        connector,
        "fetch_public_https",
        lambda _: (_ for _ in ()).throw(safe_http.CrawlerRequestError("Crawler request failed validation.")),
    )

    with pytest.raises(safe_http.CrawlerRequestError, match="failed validation"):
        list(connector.documents(crawler_context(config)))


def test_crawler_metadata_maps_to_zero_or_one_provider_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        connector,
        "fetch_public_https",
        lambda url: safe_http.PublicHttpResponse(
            content=b"<title>Page</title>", content_type="text/html", final_url=url
        ),
    )
    config = CrawlerConnectorConfiguration(
        urls=(
            CrawlUrl(
                url="https://example.com/restricted",
                acl=(CrawlAclEntry(principal_id="person@example.com", access=AccessEffect.ALLOW),),
            ),
        )
    )
    document = next(connector.documents(crawler_context(config)))

    assert S3Adapter().render_document(document, "object.html").sidecar is None
    sidecar = BedrockKnowledgeBaseAdapter().render_document(document, "object.html").sidecar
    assert sidecar is not None
    assert json.loads(sidecar.content)["accessControlList"] == [
        {"Name": "person@example.com", "Type": "USER", "Access": "ALLOW"}
    ]


@pytest.mark.parametrize(
    "address",
    ("127.0.0.1", "169.254.169.254", "10.0.0.1", "224.0.0.1", "fc00::1", "::1", "ff02::1"),
)
def test_fetch_rejects_non_global_or_multicast_address(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    target_url = "https://example.com"
    pools = _install_transport(monkeypatch, {"example.com": (address,)}, [])

    with pytest.raises(safe_http.CrawlerRequestError) as caught:
        safe_http.fetch_public_https(target_url)

    diagnostic = str(caught.value)
    assert diagnostic == "Crawler request failed validation."
    assert address not in diagnostic
    assert target_url not in diagnostic
    assert pools == []


def test_fetch_rejects_mixed_safe_and_unsafe_dns_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = _install_transport(monkeypatch, {"example.com": (_PUBLIC_ADDRESS, "127.0.0.1")}, [])

    with pytest.raises(safe_http.CrawlerRequestError, match="failed validation"):
        safe_http.fetch_public_https("https://example.com")

    assert pools == []


@pytest.mark.parametrize("answers", ((), None))
def test_fetch_fails_closed_when_dns_has_no_usable_answer(
    monkeypatch: pytest.MonkeyPatch,
    answers: tuple[str, ...] | None,
) -> None:
    if answers is None:
        monkeypatch.setattr(
            safe_http.socket,
            "getaddrinfo",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private DNS detail")),
        )
    else:
        _install_transport(monkeypatch, {"example.com": answers}, [])

    with pytest.raises(safe_http.CrawlerRequestError) as caught:
        safe_http.fetch_public_https("https://example.com")

    assert str(caught.value) == "Crawler request failed validation."


def test_fetch_closes_pool_when_request_dispatch_fails_and_sanitizes_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_detail = "sensitive upstream connection detail"

    def fail_urlopen(*_args: Any, **_kwargs: Any) -> FakeResponse:
        raise RuntimeError(sensitive_detail)

    monkeypatch.setattr(FakePool, "urlopen", fail_urlopen)
    pools = _install_transport(monkeypatch, {"example.com": (_PUBLIC_ADDRESS,)}, [])

    with pytest.raises(safe_http.CrawlerRequestError) as caught:
        safe_http.fetch_public_https("https://example.com")

    assert len(pools) == 1
    assert pools[0].closed
    assert str(caught.value) == "Crawler request failed validation."
    assert sensitive_detail not in str(caught.value)


@pytest.mark.parametrize("address", ("10.0.0.1", "224.0.0.1", "ff02::1"))
def test_fetch_revalidates_redirect_and_rejects_unsafe_target(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    rejected_redirect_url = "https://private.example/path"
    first_response = FakeResponse(status=302, headers={"Location": rejected_redirect_url})
    pools = _install_transport(
        monkeypatch,
        {"public.example": (_PUBLIC_ADDRESS,), "private.example": (address,)},
        [first_response],
    )

    with pytest.raises(safe_http.CrawlerRequestError) as caught:
        safe_http.fetch_public_https("https://public.example/start")

    diagnostic = str(caught.value)
    assert diagnostic == "Crawler request failed validation."
    assert address not in diagnostic
    assert rejected_redirect_url not in diagnostic
    assert first_response.closed
    assert [pool.host for pool in pools] == [_PUBLIC_ADDRESS]
    assert [request[1] for request in pools[0].requests] == ["/start"]
    assert pools[0].closed


def test_fetch_rejects_redirect_without_location(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(status=302)
    _install_transport(monkeypatch, {"example.com": (_PUBLIC_ADDRESS,)}, [response])

    with pytest.raises(safe_http.CrawlerRequestError) as caught:
        safe_http.fetch_public_https("https://example.com/start")

    assert str(caught.value) == "Crawler request failed validation."


def test_fetch_rejects_malformed_redirect_location(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(status=302, headers={"Location": "https://[invalid"})
    _install_transport(monkeypatch, {"example.com": (_PUBLIC_ADDRESS,)}, [response])

    with pytest.raises(safe_http.CrawlerRequestError) as caught:
        safe_http.fetch_public_https("https://example.com/start")

    assert str(caught.value) == "Crawler request failed validation."


def test_fetch_rejects_oversized_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(headers={"Content-Length": str(safe_http.MAX_RESPONSE_BYTES + 1)})
    _install_transport(monkeypatch, {"example.com": (_PUBLIC_ADDRESS,)}, [response])

    with pytest.raises(safe_http.CrawlerRequestError):
        safe_http.fetch_public_https("https://example.com")

    assert response.closed


def test_fetch_rejects_oversized_decompressed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(chunks=(b"a" * safe_http.MAX_RESPONSE_BYTES, b"b"))
    _install_transport(monkeypatch, {"example.com": (_PUBLIC_ADDRESS,)}, [response])

    with pytest.raises(safe_http.CrawlerRequestError):
        safe_http.fetch_public_https("https://example.com")

    assert response.closed


def test_fetch_resolves_relative_redirect_and_repins_host(monkeypatch: pytest.MonkeyPatch) -> None:
    redirect = FakeResponse(status=302, headers={"Location": "/final"})
    final = FakeResponse(headers={"Content-Type": "text/html"}, chunks=(b"safe",))
    pools = _install_transport(monkeypatch, {"example.com": (_PUBLIC_ADDRESS,)}, [redirect, final])

    fetched = safe_http.fetch_public_https("https://example.com/start")

    assert fetched.final_url == "https://example.com/final"
    assert [pool.requests[0][1] for pool in pools] == ["/start", "/final"]
    assert all(pool.closed for pool in pools)
    assert redirect.closed and final.closed


def test_fetch_pins_public_address_and_preserves_tls_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(headers={"Content-Type": "text/html"}, chunks=(b"safe",))
    pools = _install_transport(monkeypatch, {"example.com": (_PUBLIC_ADDRESS,)}, [response])

    fetched = safe_http.fetch_public_https("https://example.com:8443/base/path?key=value")

    assert fetched.content == b"safe"
    assert fetched.final_url == "https://example.com:8443/base/path?key=value"
    assert len(pools) == 1
    pool = pools[0]
    assert pool.host == _PUBLIC_ADDRESS
    assert pool.configuration["port"] == 8443
    assert pool.configuration["server_hostname"] == "example.com"
    assert pool.configuration["assert_hostname"] == "example.com"
    assert pool.configuration["cert_reqs"] == "CERT_REQUIRED"
    assert pool.configuration["ssl_minimum_version"] is safe_http.ssl.TLSVersion.TLSv1_2
    method, target, request = pool.requests[0]
    assert (method, target) == ("GET", "/base/path?key=value")
    assert request["headers"] == {"Host": "example.com:8443"}
    assert request["redirect"] is False
    assert request["preload_content"] is False
    assert response.closed
    assert pool.closed
