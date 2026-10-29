"""Per-provider supported file extensions and the fallback suffix rule.

A managed knowledge base ingests only certain file extensions. A document whose
extension is not supported (for example ``.java`` or ``.yaml``) is skipped unless
its object is stored under a supported extension. Both Bedrock and Amazon Quick
ingest plain text, so an unsupported document is stored with ``.txt`` appended to
its key (``report.java`` -> ``report.java.txt``), which keeps the original name
visible while making the content ingestible.

Supported sets verified against AWS documentation on 2026-10-09:
- Bedrock: knowledge-base-ds.html, "Supported document formats and limits".
- Amazon Quick: "Supported document types" documentation.
"""

_PLAIN_TEXT_SUFFIX = ".txt"

# Extensions a Bedrock managed knowledge base S3 data source ingests.
BEDROCK_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".txt", ".md", ".html", ".doc", ".docx", ".csv", ".xls", ".xlsx", ".pdf"}
)

# Extensions Amazon Quick ingests from S3.
QUICK_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".html",
        ".xml",
        ".xslt",
        ".md",
        ".csv",
        ".xls",
        ".xlsx",
        ".json",
        ".rtf",
        ".ppt",
        ".pptx",
        ".docx",
        ".txt",
    }
)


def effective_suffix(original_suffix: str, supported_extensions: frozenset[str]) -> str:
    """Return the suffix to store a document under for a provider.

    Returns ``original_suffix`` when the provider ingests it; otherwise appends
    ``.txt`` so the content is ingested as plain text. An empty suffix (a file
    with no extension) is left unchanged.
    """
    if not original_suffix or original_suffix.lower() in supported_extensions:
        return original_suffix
    return f"{original_suffix}{_PLAIN_TEXT_SUFFIX}"
