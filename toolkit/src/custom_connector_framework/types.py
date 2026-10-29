"""Content-type detection and mapping shared by provider adapters."""

from enum import Enum
from typing import ClassVar

from custom_connector_framework.models.exceptions import UnsupportedDocumentError


class ContentType(str, Enum):
    """Normalized content type, independent of any single provider's vocabulary."""

    PDF = "PDF"
    HTML = "HTML"
    XML = "XML"
    XSLT = "XSLT"
    MD = "MD"
    CSV = "CSV"
    MS_EXCEL = "MS_EXCEL"
    JSON = "JSON"
    RTF = "RTF"
    PPT = "PPT"
    MS_WORD = "MS_WORD"
    PLAIN_TEXT = "PLAIN_TEXT"


class FileExtension(str, Enum):
    """File extensions mapped to a `ContentType`."""

    PDF = "pdf"
    HTML = "html"
    HTM = "htm"
    XML = "xml"
    XSLT = "xslt"
    XSL = "xsl"
    MD = "md"
    MARKDOWN = "markdown"
    CSV = "csv"
    XLSX = "xlsx"
    XLS = "xls"
    JSON = "json"
    RTF = "rtf"
    PPTX = "pptx"
    PPT = "ppt"
    DOCX = "docx"
    DOC = "doc"
    TXT = "txt"


class MimeType(str, Enum):
    """MIME types mapped to a `ContentType`."""

    PDF = "application/pdf"
    HTML = "text/html"
    HTML_APP = "application/html"
    XML = "text/xml"
    XML_APP = "application/xml"
    XSLT = "application/xslt+xml"
    MARKDOWN = "text/markdown"
    MARKDOWN_X = "text/x-markdown"
    CSV = "text/csv"
    CSV_APP = "application/csv"
    EXCEL = "application/vnd.ms-excel"
    EXCEL_OPENXML = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    JSON = "application/json"
    RTF = "application/rtf"
    RTF_TEXT = "text/rtf"
    POWERPOINT = "application/vnd.ms-powerpoint"
    POWERPOINT_OPENXML = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    WORD = "application/msword"
    WORD_OPENXML = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    PLAIN_TEXT = "text/plain"


class DocumentTypeMapper:
    """Maps file extensions and MIME types to a normalized `ContentType`."""

    EXTENSION_TO_CONTENT_TYPE: ClassVar[dict[FileExtension, ContentType]] = {
        FileExtension.PDF: ContentType.PDF,
        FileExtension.HTML: ContentType.HTML,
        FileExtension.HTM: ContentType.HTML,
        FileExtension.XML: ContentType.XML,
        FileExtension.XSLT: ContentType.XSLT,
        FileExtension.XSL: ContentType.XSLT,
        FileExtension.MD: ContentType.MD,
        FileExtension.MARKDOWN: ContentType.MD,
        FileExtension.CSV: ContentType.CSV,
        FileExtension.XLSX: ContentType.MS_EXCEL,
        FileExtension.XLS: ContentType.MS_EXCEL,
        FileExtension.JSON: ContentType.JSON,
        FileExtension.RTF: ContentType.RTF,
        FileExtension.PPTX: ContentType.PPT,
        FileExtension.PPT: ContentType.PPT,
        FileExtension.DOCX: ContentType.MS_WORD,
        FileExtension.DOC: ContentType.MS_WORD,
        FileExtension.TXT: ContentType.PLAIN_TEXT,
    }

    MIME_TO_CONTENT_TYPE: ClassVar[dict[MimeType, ContentType]] = {
        MimeType.PDF: ContentType.PDF,
        MimeType.HTML: ContentType.HTML,
        MimeType.HTML_APP: ContentType.HTML,
        MimeType.XML: ContentType.XML,
        MimeType.XML_APP: ContentType.XML,
        MimeType.XSLT: ContentType.XSLT,
        MimeType.MARKDOWN: ContentType.MD,
        MimeType.MARKDOWN_X: ContentType.MD,
        MimeType.CSV: ContentType.CSV,
        MimeType.CSV_APP: ContentType.CSV,
        MimeType.EXCEL: ContentType.MS_EXCEL,
        MimeType.EXCEL_OPENXML: ContentType.MS_EXCEL,
        MimeType.JSON: ContentType.JSON,
        MimeType.RTF: ContentType.RTF,
        MimeType.RTF_TEXT: ContentType.RTF,
        MimeType.POWERPOINT: ContentType.PPT,
        MimeType.POWERPOINT_OPENXML: ContentType.PPT,
        MimeType.WORD: ContentType.MS_WORD,
        MimeType.WORD_OPENXML: ContentType.MS_WORD,
        MimeType.PLAIN_TEXT: ContentType.PLAIN_TEXT,
    }

    @classmethod
    def get_content_type_from_extension(cls, extension: str) -> ContentType:
        """Resolve a `ContentType` from a file extension (with or without the leading dot)."""
        try:
            file_ext = FileExtension(extension.lower().lstrip("."))
            return cls.EXTENSION_TO_CONTENT_TYPE[file_ext]
        except (ValueError, KeyError) as error:
            raise UnsupportedDocumentError(f"Unsupported file extension: {extension}") from error

    @classmethod
    def get_content_type_from_mime(cls, mime_type: str) -> ContentType:
        """Resolve a `ContentType` from a MIME type string."""
        try:
            mime = MimeType(mime_type.lower())
            return cls.MIME_TO_CONTENT_TYPE[mime]
        except (ValueError, KeyError) as error:
            raise UnsupportedDocumentError(f"Unsupported MIME type: {mime_type}") from error
