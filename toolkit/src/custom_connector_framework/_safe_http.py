"""Private validated HTTPS transport for source endpoints and public crawling."""

import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import urllib3
from pydantic import AfterValidator

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 5
_REQUEST_TIMEOUT_SECONDS = 30
_MAX_HOSTNAME_LENGTH = 253
_MAX_LABEL_LENGTH = 63
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ERROR_STATUS_MIN = 300
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_UNSAFE_RAW_URL_CHARACTER = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")
_URL_ERROR = "URL must use HTTPS with a valid hostname and no user information."
_ENDPOINT_ERROR = "Source endpoint must not contain a query or fragment."
_REQUEST_ERROR = "Crawler request failed validation."


class CrawlerRequestError(RuntimeError):
    """A sanitized crawler target, redirect, response, or transport failure."""


@dataclass(frozen=True)
class PublicHttpResponse:
    """Bounded public response data needed by the crawler."""

    content: bytes
    content_type: str
    final_url: str


def _ascii_hostname(hostname: str) -> str:
    """Validate a DNS name or IP literal and return its connection form."""
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        pass

    dns_name = hostname.removesuffix(".")
    if not dns_name or ":" in dns_name or ("." in dns_name and set(dns_name) <= set("0123456789.")):
        raise ValueError(_URL_ERROR)
    try:
        ascii_name = dns_name.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError(_URL_ERROR) from None
    if len(ascii_name) > _MAX_HOSTNAME_LENGTH:
        raise ValueError(_URL_ERROR)
    labels = ascii_name.split(".")
    if any(not label or len(label) > _MAX_LABEL_LENGTH or _HOST_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError(_URL_ERROR)
    return ascii_name


def _https_parts(value: str) -> SplitResult:
    if _UNSAFE_RAW_URL_CHARACTER.search(value) is not None:
        raise ValueError(_URL_ERROR)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ValueError(_URL_ERROR) from None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in hostname)
    ):
        raise ValueError(_URL_ERROR)
    _ascii_hostname(hostname)
    return parsed


def _validate_https_url(value: str) -> str:
    _https_parts(value)
    return value


def _validate_source_endpoint(value: str) -> str:
    parsed = _https_parts(value)
    if parsed.query or parsed.fragment:
        raise ValueError(_ENDPOINT_ERROR)
    return value


HttpsUrl = Annotated[str, AfterValidator(_validate_https_url)]
SourceEndpoint = Annotated[str, AfterValidator(_validate_source_endpoint)]


def _resolve_public_address(hostname: str, port: int) -> str:
    try:
        resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(ipaddress.ip_address(address[4][0]) for address in resolved))
    except (OSError, ValueError):
        raise CrawlerRequestError(_REQUEST_ERROR) from None
    if not addresses or any(not address.is_global or address.is_multicast for address in addresses):
        raise CrawlerRequestError(_REQUEST_ERROR)
    return str(addresses[0])


def _host_header(hostname: str, port: int, has_explicit_port: bool) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host}:{port}" if has_explicit_port else host


def _read_bounded_body(response: urllib3.BaseHTTPResponse) -> bytes:
    content_length = response.headers.get("Content-Length")
    try:
        declared_length = int(content_length) if content_length is not None else 0
        if not 0 <= declared_length <= MAX_RESPONSE_BYTES:
            raise CrawlerRequestError(_REQUEST_ERROR)
    except ValueError:
        raise CrawlerRequestError(_REQUEST_ERROR) from None
    body = bytearray()
    for chunk in response.stream(64 * 1024, decode_content=True):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CrawlerRequestError(_REQUEST_ERROR)
    return bytes(body)


def fetch_public_https(url: str) -> PublicHttpResponse:
    """Fetch a bounded body after validating and pinning each public hop.

    Direct pools bypass environment proxies. Every redirect gets a fresh DNS
    check and connection pin before a request is sent.
    """
    current_url = url
    for redirect_count in range(_MAX_REDIRECTS + 1):
        try:
            parsed = _https_parts(current_url)
            hostname = parsed.hostname
            if hostname is None:
                raise CrawlerRequestError(_REQUEST_ERROR)
            connection_hostname = _ascii_hostname(hostname)
            explicit_port = parsed.port
            port = explicit_port or 443
            address = _resolve_public_address(connection_hostname, port)
            pool = urllib3.HTTPSConnectionPool(
                address,
                port=port,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                server_hostname=connection_hostname,
                assert_hostname=connection_hostname,
                cert_reqs="CERT_REQUIRED",
                ssl_minimum_version=ssl.TLSVersion.TLSv1_2,
            )
            try:
                response = pool.urlopen(
                    "GET",
                    urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                    headers={"Host": _host_header(connection_hostname, port, explicit_port is not None)},
                    redirect=False,
                    retries=False,
                    preload_content=False,
                    decode_content=True,
                )
                try:
                    if response.status in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location or redirect_count == _MAX_REDIRECTS:
                            raise CrawlerRequestError(_REQUEST_ERROR)
                        current_url = urljoin(current_url, location)
                        _https_parts(current_url)
                        continue
                    if response.status >= _ERROR_STATUS_MIN:
                        raise CrawlerRequestError(_REQUEST_ERROR)
                    return PublicHttpResponse(
                        content=_read_bounded_body(response),
                        content_type=response.headers.get("Content-Type", "").lower(),
                        final_url=current_url,
                    )
                finally:
                    response.close()
            finally:
                pool.close()
        except CrawlerRequestError:
            raise
        except Exception:
            raise CrawlerRequestError(_REQUEST_ERROR) from None
    raise CrawlerRequestError(_REQUEST_ERROR)
