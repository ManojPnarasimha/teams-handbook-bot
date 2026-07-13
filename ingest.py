"""SharePoint ingestion pipeline."""

from __future__ import annotations

# Load dotenv before other env reads when run as a script.
import os as _os
if _os.getenv("_DOTENV_LOADED") is None and __name__ == "__main__":
    from dotenv import load_dotenv as _ld
    _ld()
    _os.environ["_DOTENV_LOADED"] = "1"

import argparse
import asyncio
import hashlib
import importlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

import sharepoint_client
from db_client import embed_text, get_supabase

logger = logging.getLogger(__name__)

# Chunking targets for RAG quality and cost.
CHUNK_TARGET_CHARS = int(os.getenv("CHUNK_TARGET_CHARS", "1200"))
CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "1800"))


# Extraction

def _extract_elements(local_path: str):
    """Extract document elements with layout awareness."""
    try:
        partition = importlib.import_module("unstructured.partition.auto").partition
    except ImportError as exc:
        raise RuntimeError("The 'unstructured' package is required for ingestion.") from exc

    return partition(
        filename=local_path,
        strategy="hi_res",
        infer_table_structure=True,
    )


def _element_text(el) -> str:
    # Keep table structure as HTML when available.
    category = getattr(el, "category", None)
    if category == "Table":
        md = getattr(el.metadata, "text_as_html", None) if getattr(el, "metadata", None) else None
        if md:
            return md
    return (getattr(el, "text", "") or "").strip()


def _chunk_by_structure(elements: Iterable) -> list[str]:
    """Group elements into chunks within the configured size limits."""
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


# DB helpers

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def delete_file_chunks(source_path: str) -> int:
    """Remove all rows for a source path."""
    sb = await get_supabase()
    res = (
        await sb.table("documents")
        .delete()
        .eq("source_path", source_path)
        .execute()
    )
    n = len(res.data or [])
    logger.info("Deleted %d chunks for %s", n, source_path)
    return n


def _download_to_tempfile(download_url: str, suffix: str) -> str:
    """Download a file to a temp path."""
    data = sharepoint_client.download_file(download_url)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".bin")
    try:
        tmp.write(data)
        tmp.flush()
    finally:
        tmp.close()
    return tmp.name


# Public API

async def process_file(file_meta: dict) -> int:
    """Process one SharePoint file end to end."""
    source_path = file_meta["path"]
    download_url = file_meta["download_url"]
    suffix = Path(file_meta["name"]).suffix

    logger.info("Processing %s", source_path)
    local_path = await asyncio.to_thread(_download_to_tempfile, download_url, suffix)
    try:
        elements = await asyncio.to_thread(_extract_elements, local_path)
        chunks = _chunk_by_structure(elements)
        if not chunks:
            logger.warning("No extractable chunks for %s", source_path)
            await delete_file_chunks(source_path)
            return 0

        # Embed chunks off the event loop and slow down for the free-tier limit.
        import time

        def _embed_with_backoff(chunks_list):
            results = []
            for i, c in enumerate(chunks_list):
                if i > 0 and i % 80 == 0:
                    logger.info("Rate-limit pause at chunk %d/%d", i, len(chunks_list))
                    time.sleep(30)
                results.append(embed_text(c))
            return results

        embeddings = await asyncio.to_thread(_embed_with_backoff, chunks)

        rows = [
            {
                "source_path": source_path,
                "source_updated_at": file_meta.get("last_modified"),
                "content": chunk,
                # Prefix with source_path to keep identical text unique.
                "content_hash": _hash(f"{source_path}::{chunk}"),
                "embedding": emb,
            }
            for chunk, emb in zip(chunks, embeddings)
        ]

        # Delete then insert to avoid stale chunks.
        await delete_file_chunks(source_path)
        sb = await get_supabase()
        await sb.table("documents").insert(rows).execute()
        logger.info("Inserted %d chunks for %s", len(rows), source_path)
        return len(rows)
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass


# SharePoint sync

async def _existing_index() -> dict[str, str | None]:
    """Return the current source_path -> updated_at index."""
    sb = await get_supabase()
    res = (
        await sb.table("documents")
        .select("source_path,source_updated_at")
        .execute()
    )
    # Narrow the typed payload before reading fields.
    data = res.data if isinstance(res.data, list) else []
    out: dict[str, str | None] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sp = row.get("source_path")
        if isinstance(sp, str) and sp not in out:
            updated = row.get("source_updated_at")
            out[sp] = updated if isinstance(updated, str) else None
    return out


async def sync_from_sharepoint() -> dict:
    """Diff SharePoint against the DB and reconcile changes."""
    logger.info("SharePoint sync starting")
    remote = await asyncio.to_thread(sharepoint_client.list_files)
    remote_by_path = {f["path"]: f for f in remote}
    existing = await _existing_index()

    to_process: list[dict] = []
    for path, meta in remote_by_path.items():
        prev = existing.get(path)
        # Compare ISO timestamps directly; Graph normalises to UTC.
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


# CLI

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
    from dotenv import load_dotenv

    load_dotenv()

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

    if args.file:
        asyncio.run(_ingest_files(args.file))
    elif args.backfill:
        asyncio.run(_backfill())
    elif args.sync:
        asyncio.run(sync_from_sharepoint())
    elif args.delete:
        asyncio.run(delete_file_chunks(args.delete))
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
