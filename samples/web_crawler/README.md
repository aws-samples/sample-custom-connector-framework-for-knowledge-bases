# Web crawler connector

The crawler yields one document for each configured URL. Its scope remains the configured pages only; it does not discover links or crawl recursively.

## Connector configuration

```json
{
  "default_acl": [
    {
      "principal_id": "person@example.com",
      "principal_type": "USER",
      "access": "ALLOW"
    }
  ],
  "urls": [
    { "url": "https://example.com/public" },
    {
      "url": "https://example.com/restricted",
      "acl": [
        {
          "principal_id": "person@example.com",
          "principal_type": "USER",
          "access": "ALLOW"
        }
      ]
    }
  ]
}
```

The connector uses the toolkit's `EmptyJobConfiguration`, so job configuration is `{}`. It fetches only configured HTML and PDF pages. Removing a URL from the configuration deletes its document on the next run, and a failed request stops the run before anything is written or deleted.

## Request safety

The crawler accepts HTTPS URLs without user information. Before every request and redirect it resolves the hostname, rejects the complete DNS answer set if any IPv4 or IPv6 address is non-global, and connects to one approved address while keeping the original hostname for TLS SNI and certificate checks. It bypasses environment proxies, follows at most five redirects, and limits both the declared `Content-Length` and the decompressed streamed content to 10 MiB. Errors are static and omit URLs, response content, configuration, and ACL identities.

## Runtime

CCF runs this definition with:

```text
ccf-connector run web_crawler.connector:definition
```
