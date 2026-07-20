"""SharePoint → Azure Document Intelligence / PyMuPDF → Azure SQL ingestion."""

from __future__ import annotations

# Load .env before any module-level os.getenv() calls below.
from dotenv import load_dotenv
load_dotenv()

import argparse
import asyncio
import hashlib
import logging
import os
import re
from typing import Iterable

import db
import sharepoint_client
from document_extractor import LayoutBlock, extract_layout
from embeddings import embed_many

logger = logging.getLogger(__name__)

# Chunking targets. Smaller = finer retrieval; tune via env.
CHUNK_TARGET_CHARS = int(os.getenv("CHUNK_TARGET_CHARS", "500"))
CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "800"))
CHUNK_OVERLAP_CHARS = int(os.getenv("CHUNK_OVERLAP_CHARS", "80"))
CHUNK_MIN_CHARS = int(os.getenv("CHUNK_MIN_CHARS", "80"))


# ---- Chunking --------------------------------------------------------------

def _split_long_text(text: str, max_chars: int) -> list[str]:
    """Recursively split an oversized block at natural boundaries.

    Tries paragraphs -> sentences -> whitespace -> hard character slice.
    Guarantees every returned piece is <= max_chars.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # 1. Paragraph boundaries (blank lines).
    parts = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) > 1:
        out: list[str] = []
        for p in parts:
            out.extend(_split_long_text(p, max_chars))
        return out

    # 2. Sentence boundaries.
    parts = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(parts) > 1:
        out = []
        for p in parts:
            out.extend(_split_long_text(p, max_chars))
        return out

    # 3. Whitespace / word boundaries, packing words up to max_chars.
    words = text.split()
    if len(words) > 1:
        out = []
        buf: list[str] = []
        buf_len = 0
        for w in words:
            add_len = len(w) + (1 if buf else 0)
            if buf and buf_len + add_len > max_chars:
                out.append(" ".join(buf))
                buf, buf_len = [w], len(w)
            else:
                buf.append(w)
                buf_len += add_len
        if buf:
            out.append(" ".join(buf))
        return out

    # 4. Hard slice (single very long token, e.g. base64 blob).
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def _append_with_overlap(chunks: list[str], new_chunk: str) -> None:
    """Append new_chunk, prefixing it with the tail of the previous chunk.

    Overlap gives the embedder shared context across boundaries so a query
    that straddles two chunks still matches at least one.
    """
    if not new_chunk:
        return
    if CHUNK_OVERLAP_CHARS > 0 and chunks:
        tail = chunks[-1][-CHUNK_OVERLAP_CHARS:]
        if tail and not new_chunk.startswith(tail):
            new_chunk = f"{tail} {new_chunk}"
    chunks.append(new_chunk)


def _chunk_by_structure(blocks: Iterable[LayoutBlock]) -> list[str]:
    """Group layout blocks into overlapping chunks within size targets.

    Strategy:
      * Every heading starts a new chunk (preserves section context).
      * Blocks larger than CHUNK_MAX_CHARS are split internally at
        paragraph -> sentence -> word boundaries before grouping.
      * Tables are flushed to their own chunk(s); very large tables are
        split by rows so no single chunk exceeds CHUNK_MAX_CHARS, and the
        markdown header is repeated on each piece for context.
      * Adjacent chunks share a small CHUNK_OVERLAP_CHARS tail so a query
        crossing a boundary still hits at least one chunk.
      * Very short trailing chunks are merged into the previous chunk to
        avoid noisy micro-fragments.
    """
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        chunk = "\n\n".join(buf).strip()
        buf, buf_len = [], 0
        if not chunk:
            return
        # Merge a tiny trailing chunk into the previous one when it fits.
        if len(chunk) < CHUNK_MIN_CHARS and chunks:
            merged = f"{chunks[-1]}\n\n{chunk}"
            if len(merged) <= CHUNK_MAX_CHARS + CHUNK_OVERLAP_CHARS:
                chunks[-1] = merged
                return
        _append_with_overlap(chunks, chunk)

    def add_piece(piece: str) -> None:
        """Add a size-bounded piece to the buffer, flushing as needed."""
        nonlocal buf, buf_len
        piece = piece.strip()
        if not piece:
            return
        projected = buf_len + len(piece) + (2 if buf else 0)
        if buf and projected > CHUNK_MAX_CHARS:
            flush()
            projected = len(piece)
        buf.append(piece)
        buf_len = projected
        if buf_len >= CHUNK_TARGET_CHARS:
            flush()

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue

        if block.kind == "table":
            flush()
            lines = text.splitlines()
            # Small tables or non-markdown tables: use the generic splitter.
            if len(text) <= CHUNK_MAX_CHARS or len(lines) < 4:
                for piece in _split_long_text(text, CHUNK_MAX_CHARS):
                    _append_with_overlap(chunks, piece)
                continue
            # Large markdown tables: repeat the header on every row-piece.
            header = "\n".join(lines[:2])  # "| a | b |" + "| --- | --- |"
            current = [header]
            current_len = len(header)
            for row in lines[2:]:
                row_len = len(row) + 1
                if current_len + row_len > CHUNK_MAX_CHARS and len(current) > 1:
                    _append_with_overlap(chunks, "\n".join(current))
                    current = [header, row]
                    current_len = len(header) + row_len
                else:
                    current.append(row)
                    current_len += row_len
            if len(current) > 1:
                _append_with_overlap(chunks, "\n".join(current))
            continue

        if block.kind == "heading":
            # Section boundary: always start a fresh chunk.
            flush()

        if len(text) > CHUNK_MAX_CHARS:
            flush()
            for piece in _split_long_text(text, CHUNK_MAX_CHARS):
                add_piece(piece)
        else:
            add_piece(text)

    flush()
    return [c for c in chunks if c]


# ---- DB helpers ------------------------------------------------------------

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def delete_file_chunks(source_path: str) -> int:
    """Remove all rows for a source path."""
    n = await db.execute(
        "DELETE FROM dbo.documents WHERE source_path = ?", (source_path,)
    )
    logger.info("Deleted %d chunks for %s", n, source_path)
    return n


async def _existing_index() -> dict[str, str | None]:
    """Return source_path -> latest source_updated_at, one row per file."""
    rows = await db.fetch_all(
        """
        SELECT source_path, MAX(source_updated_at) AS updated_at
        FROM dbo.documents
        GROUP BY source_path
        """
    )
    out: dict[str, str | None] = {}
    for row in rows:
        sp = row.get("source_path")
        if not isinstance(sp, str):
            continue
        updated = row.get("updated_at")
        # DATETIMEOFFSET comes back as a datetime; normalise to ISO string.
        out[sp] = updated.isoformat() if updated is not None else None
    return out


# ---- Ingestion pipeline ----------------------------------------------------

async def process_file(file_meta: dict) -> int:
    """Ingest one SharePoint file end-to-end."""
    source_path = file_meta["path"]
    download_url = file_meta["download_url"]

    logger.info("Processing %s", source_path)
    file_bytes = await asyncio.to_thread(sharepoint_client.download_file, download_url)

    blocks = await asyncio.to_thread(extract_layout, file_bytes)
    chunks = _chunk_by_structure(blocks)
    if not chunks:
        logger.warning("No extractable chunks for %s", source_path)
        await delete_file_chunks(source_path)
        return 0

    embeddings = await embed_many(chunks)

    rows = [
        (
            source_path,
            file_meta.get("last_modified"),
            chunk,
            # Prefix with source_path so identical text across files stays unique.
            _hash(f"{source_path}::{chunk}"),
            db.vector_literal(emb),
        )
        for chunk, emb in zip(chunks, embeddings)
    ]

    # Delete-then-insert to avoid stale chunks.
    await delete_file_chunks(source_path)
    await db.executemany(
        """
        INSERT INTO dbo.documents
            (source_path, source_updated_at, content, content_hash, embedding)
        VALUES (?, ?, ?, ?, CAST(CONVERT(NVARCHAR(MAX), ?) AS VECTOR(768)))
        """,
        rows,
    )
    logger.info("Inserted %d chunks for %s", len(rows), source_path)
    return len(rows)


async def sync_from_sharepoint() -> dict:
    """Incremental diff: fetch remote list, compare to DB, reconcile."""
    logger.info("SharePoint sync starting")
    remote = await asyncio.to_thread(sharepoint_client.list_files)
    remote_by_path = {f["path"]: f for f in remote}
    existing = await _existing_index()

    to_process: list[dict] = []
    for path, meta in remote_by_path.items():
        prev = existing.get(path)
        if prev is None or (meta.get("last_modified") and meta["last_modified"] != prev):
            to_process.append(meta)

    to_delete = [p for p in existing.keys() if p not in remote_by_path]

    processed = 0
    for meta in to_process:
        try:
            await process_file(meta)
            processed += 1
        except Exception as e:
            logger.exception("Failed to process %s: %s", meta.get("path"), e)

    deleted = 0
    for path in to_delete:
        try:
            await delete_file_chunks(path)
            deleted += 1
        except Exception as e:
            logger.exception("Failed to delete chunks for %s: %s", path, e)

    summary = {
        "remote_files": len(remote_by_path),
        "processed": processed,
        "deleted_paths": deleted,
    }
    logger.info("SharePoint sync done: %s", summary)
    return summary


# ---- CLI -------------------------------------------------------------------

async def _backfill() -> None:
    """Process every SharePoint file, ignoring existing DB state."""
    remote = await asyncio.to_thread(sharepoint_client.list_files)
    logger.info("Backfilling %d files from SharePoint", len(remote))
    for meta in remote:
        try:
            await process_file(meta)
        except Exception as e:
            logger.exception("Failed to process %s: %s", meta.get("path"), e)


async def _ingest_files(paths: list[str]) -> None:
    """Ingest only the specified file paths from SharePoint."""
    remote = await asyncio.to_thread(sharepoint_client.list_files)
    remote_by_path = {f["path"]: f for f in remote}
    for path in paths:
        meta = remote_by_path.get(path)
        if meta is None:
            logger.error("File not found in SharePoint: %s", path)
            continue
        try:
            await process_file(meta)
        except Exception as e:
            logger.exception("Failed to process %s: %s", path, e)


def _main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Manual SharePoint ingestion helper.")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Process every file in SharePoint (ignore DB state).",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Incremental diff sync (same as the scheduled endpoint).",
    )
    parser.add_argument(
        "--delete",
        help="Delete chunks for a single source_path.",
    )
    parser.add_argument(
        "--file",
        nargs="+",
        metavar="PATH",
        help="Ingest only the specified SharePoint file path(s). "
             "e.g. --file 'HR Documents/handbook.pdf' 'Policies/leave.pdf'",
    )
    args = parser.parse_args()

    async def _run() -> None:
        try:
            if args.file:
                await _ingest_files(args.file)
            elif args.backfill:
                await _backfill()
            elif args.sync:
                await sync_from_sharepoint()
            elif args.delete:
                await delete_file_chunks(args.delete)
            else:
                parser.print_help()
        finally:
            await db.close_pool()

    asyncio.run(_run())


if __name__ == "__main__":
    _main()
