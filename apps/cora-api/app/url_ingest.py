"""Single-page URL fetch + readable-content extraction for URL Knowledge
Ingestion.

Non-crawling: fetches exactly one URL and extracts readable text — HTML,
plain text, or readable PDFs (e.g. arxiv.org/pdf links). Arbitrary binary
content is rejected. No links are followed beyond redirects.
"""

import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 30.0
MAX_CONTENT_CHARS = 40000
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MiB cap on downloaded PDFs
_USER_AGENT = "Cora-Knowledge/0.1 (+url ingestion)"

# Textual content handled by the HTML/text path.
_SUPPORTED_TEXT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
)


class UrlIngestError(Exception):
    """Raised on any fetch/extract failure. `code` maps to an HTTP status in
    the router."""

    def __init__(self, message: str, *, code: str = "fetch_failed"):
        super().__init__(message)
        self.code = code


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise UrlIngestError("url is required", code="invalid_url")
    # Default to https:// when the user omits a scheme.
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UrlIngestError(
            f"unsupported URL scheme {parts.scheme!r}; only http/https allowed",
            code="invalid_url",
        )
    if not parts.netloc:
        raise UrlIngestError("url has no host", code="invalid_url")
    return urlunsplit(parts)


def _normalize_whitespace(text: str) -> str:
    lines = [
        re.sub(r"[ \t ]+", " ", line).strip()
        for line in (text or "").splitlines()
    ]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line:
            out.append(line)
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


def _extract(html: str, fallback_title: str) -> tuple[str, str]:
    """Return (title, text). Prefer readability; fall back to BeautifulSoup."""
    from bs4 import BeautifulSoup

    title = fallback_title
    text = ""

    # 1. readability-lxml for article extraction.
    try:
        from readability import Document

        doc = Document(html)
        short = (doc.short_title() or "").strip()
        if short:
            title = short
        summary_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(summary_html, "lxml")
        text = soup.get_text(separator="\n")
    except Exception:
        logger.debug("readability extraction failed; falling back", exc_info=True)
        text = ""

    # 2. Fallback: strip noise and take body text.
    if not text.strip():
        soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            t = soup.title.string.strip()
            if t and (not title or title == fallback_title):
                title = t
        for tag in soup(
            ["script", "style", "noscript", "nav", "header", "footer",
             "aside", "svg", "form", "iframe"]
        ):
            tag.decompose()
        body = soup.body or soup
        text = body.get_text(separator="\n")

    return (title or fallback_title).strip(), _normalize_whitespace(text)


def _url_filename_title(final_url: str) -> str:
    """Derive a title from the URL's path basename (extension stripped)."""
    path = urlsplit(final_url).path.rstrip("/")
    base = path.rsplit("/", 1)[-1] if path else ""
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return base.strip() or (urlsplit(final_url).netloc or final_url)


def _extract_pdf(pdf_bytes: bytes, final_url: str) -> tuple[str, str, int]:
    """Return (title, text, page_count) from PDF bytes using pypdf.

    Title preference here is metadata title → URL filename → "Untitled PDF".
    (A user-supplied title is layered on top by the endpoint.)
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = reader.pages
        page_count = len(pages)
        parts: list[str] = []
        for page in pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                logger.debug("pypdf page extract failed", exc_info=True)
        meta_title = ""
        try:
            if reader.metadata and reader.metadata.title:
                meta_title = str(reader.metadata.title).strip()
        except Exception:
            meta_title = ""
    except Exception as exc:
        raise UrlIngestError(
            f"could not parse PDF: {exc}", code="pdf_extract_failed"
        ) from exc

    text = _normalize_whitespace("\n".join(parts))
    title = meta_title or _url_filename_title(final_url) or "Untitled PDF"
    return title[:300], text, page_count


async def fetch_and_extract(url: str) -> dict:
    """Fetch a single URL and extract readable content (HTML, plain text, or PDF).

    Returns: {url (final), title, content, status_code, content_type,
              extraction_method, page_count (int|None), fetched_at,
              raw_length, truncated}. Raises UrlIngestError on any failure.
    """
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise UrlIngestError(f"could not fetch URL: {exc}", code="fetch_failed") from exc

    final_url = str(resp.url)
    status_code = resp.status_code
    content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    fetched_at = datetime.now(timezone.utc).isoformat()

    if status_code >= 400:
        raise UrlIngestError(
            f"URL returned HTTP {status_code}", code="fetch_failed"
        )

    body = resp.content
    if not body:
        raise UrlIngestError("the URL returned an empty response", code="empty")

    fallback_title = urlsplit(final_url).netloc or final_url
    is_pdf = "pdf" in content_type or body[:5] == b"%PDF-"

    if is_pdf:
        if len(body) > MAX_PDF_BYTES:
            raise UrlIngestError(
                f"PDF is too large ({len(body)} bytes); max {MAX_PDF_BYTES}",
                code="pdf_too_large",
            )
        title, text, page_count = _extract_pdf(body, final_url)
        if not text.strip():
            raise UrlIngestError(
                "no extractable text found in the PDF (it may be scanned/image-only)",
                code="no_content",
            )
        extraction_method = "pypdf"
        out_content_type = "application/pdf"
        raw_length = len(body)
    else:
        if content_type and not content_type.startswith(_SUPPORTED_TEXT_TYPES):
            raise UrlIngestError(
                f"unsupported content type {content_type!r}; "
                "this endpoint ingests HTML, plain-text, and PDF pages only "
                "(no other binary files)",
                code="unsupported_content_type",
            )
        html = resp.text
        if not html or not html.strip():
            raise UrlIngestError(
                "the URL returned an empty response", code="empty"
            )
        title, text = _extract(html, fallback_title)
        if not text.strip():
            raise UrlIngestError(
                "no readable content could be extracted from the page",
                code="no_content",
            )
        extraction_method = "readability"
        out_content_type = content_type or "text/html"
        page_count = None
        raw_length = len(html)

    truncated = len(text) > MAX_CONTENT_CHARS
    if truncated:
        cut = text[:MAX_CONTENT_CHARS].rsplit("\n", 1)[0] or text[:MAX_CONTENT_CHARS]
        text = cut.rstrip() + "\n\n[content truncated]"

    return {
        "url": final_url,
        "title": (title or fallback_title)[:300],
        "content": text,
        "status_code": status_code,
        "content_type": out_content_type,
        "extraction_method": extraction_method,
        "page_count": page_count,
        "fetched_at": fetched_at,
        "raw_length": raw_length,
        "truncated": truncated,
    }
