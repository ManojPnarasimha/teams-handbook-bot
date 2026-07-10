"""Ingestion:
- process_file(bucket_path): download → extract (unstructured hi_res) → chunk → embed →
  delete existing rows for this source_path → insert fresh chunks.
- delete_file_chunks(bucket_path): remove all rows for that source_path.
- Standalone `python ingest.py --backfill` walks the whole 'org-docs' bucket.

The delete-then-insert strategy is deliberate: changed text produces new content_hash
values, so a plain upsert would leave stale chunks alongside the new ones.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

from db_client import STORAGE_BUCKET, embed_text, get_supabase

logger = logging.getLogger(__name__)

# Chunking targets — tuned for RAG recall vs prompt cost.
CHUNK_TARGET_CHARS = int(os.getenv("CHUNK_TARGET_CHARS", "1200"))
CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "1800"))


# ---- Extraction --------------------------------------------------------------

def _extract_elements(local_path: str):
    """Layout-aware extraction. `hi_res` preserves tables/titles/lists structurally."""
    from unstructured.partition.auto import partition

    return partition(
        filename=local_path,
        strategy="hi_res",
        infer_table_structure=True,
    )


def _element_text(el) -> str:
    # Prefer HTML representation for tables so structure survives into the LLM prompt.
    category = getattr(el, "category", None)
    if category == "Table":
        md = getattr(el.metadata, "text_as_html", None) if getattr(el, "metadata", None) else None
        if md:
            return md
    return (getattr(el, "text", "") or "").strip()


def _chunk_by_structure(elements: Iterable) -> list[str]:
    """Group elements into chunks bounded by CHUNK_TARGET_CHARS.

    Rules:
    - Never split a Table across chunks (it goes in whole, even if it exceeds target).
    - Start a new chunk on a Title if the current chunk is already non-trivial.
    - Never exceed CHUNK_MAX_CHARS unless a single element is itself larger.
    """
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if buf:
            chunks.append("\n\n".join(buf).strip())
            buf, buf_len = [], 0

    for el in elements:
        text = _element_text(el)
        if not text:
            continue
        category = getattr(el, "category", None)

        if category == "Table":
            flush()
            chunks.append(text)
            continue

        if category == "Title" and buf_len > 200:
            flush()

        projected = buf_len + len(text) + 2
        if projected > CHUNK_MAX_CHARS and buf:
            flush()

        buf.append(text)
        buf_len += len(text) + 2

        if buf_len >= CHUNK_TARGET_CHARS:
            flush()

    flush()
    return [c for c in chunks if c]


# ---- DB helpers --------------------------------------------------------------

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def delete_file_chunks(bucket_path: str) -> int:
    """Remove all documents rows for this source_path. Returns rows deleted."""
    sb = await get_supabase()
    res = (
        await sb.table("documents")
        .delete()
        .eq("source_path", bucket_path)
        .execute()
    )
    n = len(res.data or [])
    logger.info("Deleted %d chunks for %s", n, bucket_path)
    return n


async def _download_to_tempfile(bucket_path: str) -> str:
    sb = await get_supabase()
    # supabase-py returns raw bytes.
    data = await sb.storage.from_(STORAGE_BUCKET).download(bucket_path)
    suffix = Path(bucket_path).suffix or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(data)
        tmp.flush()
    finally:
        tmp.close()
    return tmp.name


async def _fetch_object_updated_at(bucket_path: str) -> str | None:
    """Best-effort read of the storage object's updated_at for provenance."""
    sb = await get_supabase()
    try:
        parent = str(Path(bucket_path).parent) if "/" in bucket_path else ""
        name = Path(bucket_path).name
        res = await sb.storage.from_(STORAGE_BUCKET).list(
            parent, {"search": name, "limit": 1}
        )
        if res:
            row = res[0]
            return row.get("updated_at") or row.get("created_at")
    except Exception:
        return None
    return None


# ---- Public API --------------------------------------------------------------

async def process_file(bucket_path: str) -> int:
    """Full pipeline for one bucket object. Returns number of chunks inserted."""
    logger.info("Processing %s", bucket_path)
    local_path = await _download_to_tempfile(bucket_path)
    try:
        elements = await asyncio.to_thread(_extract_elements, local_path)
        chunks = _chunk_by_structure(elements)
        if not chunks:
            logger.warning("No extractable chunks for %s", bucket_path)
            await delete_file_chunks(bucket_path)
            return 0

        # Embed all chunks (blocking SDK) off the event loop.
        embeddings = await asyncio.to_thread(lambda: [embed_text(c) for c in chunks])

        updated_at = await _fetch_object_updated_at(bucket_path)

        rows = [
            {
                "source_path": bucket_path,
                "source_updated_at": updated_at,
                "content": chunk,
                "content_hash": _hash(f"{bucket_path}::{chunk}"),
                "embedding": emb,
            }
            for chunk, emb in zip(chunks, embeddings)
        ]

        # Delete-then-insert to guarantee no stale chunks remain for this source_path.
        await delete_file_chunks(bucket_path)
        sb = await get_supabase()
        await sb.table("documents").insert(rows).execute()
        logger.info("Inserted %d chunks for %s", len(rows), bucket_path)
        return len(rows)
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass


# ---- Backfill CLI ------------------------------------------------------------

async def _list_all_paths(prefix: str = "") -> list[str]:
    """Recursively list every file in STORAGE_BUCKET."""
    sb = await get_supabase()
    out: list[str] = []
    stack = [prefix]
    while stack:
        current = stack.pop()
        entries = await sb.storage.from_(STORAGE_BUCKET).list(current, {"limit": 1000})
        for entry in entries or []:
            name = entry.get("name")
            if not name:
                continue
            full = f"{current}/{name}" if current else name
            # Folders have no id in Supabase storage listings.
            if entry.get("id") is None and entry.get("metadata") is None:
                stack.append(full)
            else:
                out.append(full)
    return out


async def backfill() -> None:
    paths = await _list_all_paths()
    logger.info("Backfilling %d objects from bucket %s", len(paths), STORAGE_BUCKET)
    for p in paths:
        try:
            await process_file(p)
        except Exception as e:
            logger.exception("Failed to process %s: %s", p, e)


def _main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Manual ingestion helper.")
    parser.add_argument("--backfill", action="store_true", help="Process the whole bucket.")
    parser.add_argument("--path", help="Process a single bucket path.")
    parser.add_argument("--delete", help="Delete chunks for a single bucket path.")
    args = parser.parse_args()

    if args.backfill:
        asyncio.run(backfill())
    elif args.path:
        asyncio.run(process_file(args.path))
    elif args.delete:
        asyncio.run(delete_file_chunks(args.delete))
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
