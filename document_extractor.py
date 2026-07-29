"""Document layout extractor.

Primary path uses PyMuPDF to read embedded (selectable) PDF text directly:
it is fast, free, needs no OCR, and captures all text on digital PDFs -
including heavily designed ones where Azure Document Intelligence's markdown
output can come back nearly empty.

Fallback path uses Azure Document Intelligence `prebuilt-layout` (OCR) for
scanned / image-only PDFs where PyMuPDF finds little or no embedded text.

Either way we return a flat list of typed `LayoutBlock`s in reading order;
`ingest.py` groups these into chunks with its existing size policy.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat
from azure.core.credentials import AzureKeyCredential

logger = logging.getLogger(__name__)

ENDPOINT = os.getenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "")
KEY = os.getenv("AZURE_DOC_INTELLIGENCE_KEY", "")
MODEL_ID = os.getenv("AZURE_DOC_INTELLIGENCE_MODEL", "prebuilt-layout")

# If PyMuPDF extracts at least this many characters, we trust the direct text
# and skip the Azure DI OCR fallback. Scanned/image PDFs fall below this and
# trigger the fallback. Tune via env if needed.
PYMUPDF_MIN_CHARS = int(os.getenv("PYMUPDF_MIN_CHARS", "200"))


BlockKind = Literal["heading", "paragraph", "table"]


@dataclass
class LayoutBlock:
    """A single ordered element from a document."""
    kind: BlockKind
    text: str
    page: int | None = None


_client: DocumentIntelligenceClient | None = None


def _get_client() -> DocumentIntelligenceClient:
    """Lazy singleton so importing without env doesn't fail."""
    global _client
    if _client is not None:
        return _client
    endpoint = os.getenv("AZURE_DOC_INTELLIGENCE_ENDPOINT")
    key = os.getenv("AZURE_DOC_INTELLIGENCE_KEY")
    if not endpoint or not key:
        raise RuntimeError(
            "AZURE_DOC_INTELLIGENCE_ENDPOINT and AZURE_DOC_INTELLIGENCE_KEY must be set."
        )
    _client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    return _client


def _table_to_markdown(table) -> str:
    """Render a Document Intelligence table as pipe-delimited markdown."""
    rows = table.row_count
    cols = table.column_count
    grid: list[list[str]] = [["" for _ in range(cols)] for _ in range(rows)]
    for cell in table.cells or []:
        r = cell.row_index
        c = cell.column_index
        if 0 <= r < rows and 0 <= c < cols:
            grid[r][c] = (cell.content or "").replace("|", "\\|").replace("\n", " ").strip()
    if not grid:
        return ""
    header = "| " + " | ".join(grid[0]) + " |"
    sep = "| " + " | ".join(["---"] * cols) + " |"
    body = "\n".join("| " + " | ".join(row) + " |" for row in grid[1:])
    return "\n".join([header, sep, body]).strip()


# ---- Markdown parsing ------------------------------------------------------
#
# When output_content_format=MARKDOWN, DI returns a rich markdown rendering of
# the whole document in result.content. That is far more complete than
# result.paragraphs on styled/designed PDFs, so we prefer parsing it directly.

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FIGURE_TAG_RE = re.compile(r"</?figure[^>]*>", re.IGNORECASE)
_PAGE_NUMBER_RE = re.compile(r"PageNumber=\"?(\d+)\"?", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _parse_markdown_blocks(markdown: str) -> list[LayoutBlock]:
    """Split DI's markdown output into ordered LayoutBlocks.

    Recognised structures:
      * Markdown headings (``# ... ######``) -> heading blocks.
      * Runs of pipe-delimited lines -> table blocks.
      * Everything else, separated by blank lines -> paragraph blocks.

    Tracks the current page via DI's ``<!-- PageNumber="n" -->`` markers so
    downstream consumers can still cite pages when useful.

    Guarantees no visible text is dropped: PageNumber markers are captured
    first, then all HTML comments are stripped from every line before the
    line is classified, so content sharing a line with a comment is kept.
    """
    # Drop <figure> tags but keep their captions/text.
    markdown = _FIGURE_TAG_RE.sub("", markdown)

    blocks: list[LayoutBlock] = []
    para_buf: list[str] = []
    current_page: int | None = None

    def flush_paragraph() -> None:
        if not para_buf:
            return
        text = "\n".join(para_buf).strip()
        para_buf.clear()
        if text:
            blocks.append(LayoutBlock(kind="paragraph", text=text, page=current_page))

    def update_page(raw: str) -> None:
        nonlocal current_page
        for m in _PAGE_NUMBER_RE.finditer(raw):
            try:
                current_page = int(m.group(1))
            except ValueError:
                pass

    def clean(raw: str) -> str:
        """Capture page markers, then strip HTML comments from a line."""
        update_page(raw)
        return _HTML_COMMENT_RE.sub("", raw).rstrip()

    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        cleaned = clean(lines[i])
        stripped = cleaned.strip()

        # Blank line (or a line that was entirely HTML comments): boundary.
        if not stripped:
            flush_paragraph()
            i += 1
            continue

        # Heading.
        h = _HEADING_RE.match(stripped)
        if h:
            flush_paragraph()
            heading_text = h.group(2).strip()
            if heading_text:
                blocks.append(LayoutBlock(kind="heading", text=heading_text, page=current_page))
            i += 1
            continue

        # Markdown table: consecutive lines starting with '|' after cleaning.
        if stripped.startswith("|"):
            flush_paragraph()
            table_lines: list[str] = []
            while i < len(lines):
                row = clean(lines[i])
                if not row.strip().startswith("|"):
                    break
                table_lines.append(row)
                i += 1
            table_text = "\n".join(table_lines).strip()
            if table_text:
                blocks.append(LayoutBlock(kind="table", text=table_text, page=current_page))
            continue

        # Regular content line.
        para_buf.append(cleaned)
        i += 1

    flush_paragraph()
    return blocks


def _extract_from_paragraphs(result) -> list[LayoutBlock]:
    """Legacy path: build blocks from result.paragraphs + result.tables."""
    blocks: list[LayoutBlock] = []
    heading_roles = {"title", "sectionHeading", "pageHeader"}
    for p in result.paragraphs or []:
        text = (p.content or "").strip()
        if not text:
            continue
        page = None
        if p.bounding_regions:
            page = p.bounding_regions[0].page_number
        role = getattr(p, "role", None)
        kind: BlockKind = "heading" if role in heading_roles else "paragraph"
        blocks.append(LayoutBlock(kind=kind, text=text, page=page))

    for t in result.tables or []:
        md = _table_to_markdown(t)
        if not md:
            continue
        page = None
        if t.bounding_regions:
            page = t.bounding_regions[0].page_number
        blocks.append(LayoutBlock(kind="table", text=md, page=page))

    return blocks


def _extract_with_document_intelligence(file_bytes: bytes) -> list[LayoutBlock]:
    """OCR fallback: analyze the document with Azure DI `prebuilt-layout`.

    Primary path parses ``result.content`` (DI's full markdown rendering) so
    designed / multi-column PDFs contribute all their visible text. Falls back
    to ``result.paragraphs`` + ``result.tables`` only when the markdown is
    empty or produced no blocks.
    """
    client = _get_client()
    poller = client.begin_analyze_document(
        model_id=MODEL_ID,
        body=AnalyzeDocumentRequest(bytes_source=file_bytes),
        output_content_format=DocumentContentFormat.MARKDOWN,
    )
    result = poller.result()

    markdown = (result.content or "").strip()
    if markdown:
        blocks = _parse_markdown_blocks(markdown)
        if blocks:
            logger.info(
                "Document Intelligence extracted %d blocks (%d markdown chars)",
                len(blocks),
                len(markdown),
            )
            return blocks

    blocks = _extract_from_paragraphs(result)
    logger.info(
        "Document Intelligence extracted %d blocks (paragraph fallback)", len(blocks)
    )
    return blocks


def _extract_with_pymupdf(file_bytes: bytes) -> list[LayoutBlock]:
    """Primary path: read embedded PDF text directly with PyMuPDF.

    Preserves reading order and paragraph boundaries (one text block per
    PyMuPDF block) and infers headings from font size / bold weight. Captures
    all selectable text, including table cell text (flattened to paragraph
    text rather than a markdown grid). Returns an empty list for scanned PDFs
    with no embedded text, which signals the caller to use the OCR fallback.
    """
    import fitz  # PyMuPDF; imported lazily so the module loads without it.

    doc = fitz.open(stream=file_bytes, filetype="pdf")

    # Pass 1: estimate the body font size (most common size, weighted by the
    # number of characters at that size) so headings can be detected relative
    # to it rather than against an absolute threshold.
    size_weights: Counter[int] = Counter()
    page_dicts: list[dict] = []
    for page in doc:
        d: dict = page.get_text("dict")  # type: ignore[assignment]
        page_dicts.append(d)
        for block in d.get("blocks", []):
            if block.get("type") != 0:  # 0 = text, 1 = image
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if text.strip():
                        size_weights[round(span.get("size", 0.0))] += len(text)
    body_size = max(size_weights, key=size_weights.__getitem__) if size_weights else 0

    # Pass 2: build blocks in reading order.
    blocks: list[LayoutBlock] = []
    for page_no, d in enumerate(page_dicts, start=1):
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            line_texts: list[str] = []
            max_size = 0.0
            is_bold = False
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                line_text = "".join(s.get("text", "") for s in spans)
                if line_text.strip():
                    line_texts.append(line_text.rstrip())
                for s in spans:
                    if s.get("text", "").strip():
                        max_size = max(max_size, float(s.get("size", 0.0)))
                        if int(s.get("flags", 0)) & 16:  # bit 4 = bold
                            is_bold = True
            text = "\n".join(line_texts).strip()
            if not text:
                continue
            # Heading heuristic: short, single-line, and visually emphasised.
            single_line = len(line_texts) == 1
            emphasised = max_size >= body_size + 1.0 or (is_bold and max_size >= body_size)
            kind: BlockKind = (
                "heading" if single_line and len(text) <= 160 and emphasised else "paragraph"
            )
            blocks.append(LayoutBlock(kind=kind, text=text, page=page_no))

    return blocks


def extract_layout(file_bytes: bytes) -> list[LayoutBlock]:
    """Return ordered layout blocks for a document.

    Tries direct PyMuPDF text extraction first (fast, complete, no OCR). If
    that yields too little text - typically a scanned or image-only PDF -
    falls back to Azure Document Intelligence OCR.
    """
    try:
        blocks = _extract_with_pymupdf(file_bytes)
    except Exception as e:  # ImportError, corrupt/non-PDF, etc.
        logger.warning("PyMuPDF extraction failed (%s); using Azure DI", e)
        blocks = []

    total_chars = sum(len(b.text) for b in blocks)
    if blocks and total_chars >= PYMUPDF_MIN_CHARS:
        logger.info("PyMuPDF extracted %d blocks (%d chars)", len(blocks), total_chars)
        return blocks

    logger.info(
        "PyMuPDF yielded %d chars (< %d); falling back to Azure Document Intelligence",
        total_chars,
        PYMUPDF_MIN_CHARS,
    )
    return _extract_with_document_intelligence(file_bytes)
