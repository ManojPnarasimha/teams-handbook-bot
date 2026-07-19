"""Azure SQL Database async client.

A tiny wrapper around aioodbc that gives us a lazy connection pool and a few
typed helpers. Kept intentionally small: everything else in the codebase just
calls execute / fetch_one / fetch_all / executemany.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import struct
from typing import Any, Sequence

import aioodbc

logger = logging.getLogger(__name__)

# ---- DATETIMEOFFSET decoding -----------------------------------------------
#
# pyodbc has no built-in decoder for SQL Server's DATETIMEOFFSET type (ODBC
# type -155 / SQL_SS_TIMESTAMPOFFSET); every schema column that uses it
# (created_at, updated_at, source_updated_at, ...) fails with
# "ODBC SQL type -155 is not yet supported" unless we register an output
# converter on each pooled connection.

SQL_SS_TIMESTAMPOFFSET = -155


def _decode_datetimeoffset(raw: bytes) -> datetime.datetime:
    # Wire format: 6 shorts (y, m, d, hh, mm, ss), 1 uint (ns), 2 shorts (tz hh, tz mm)
    year, month, day, hour, minute, second, nanoseconds, tz_hour, tz_minute = (
        struct.unpack("<6hI2h", raw)
    )
    return datetime.datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        nanoseconds // 1000,
        datetime.timezone(datetime.timedelta(hours=tz_hour, minutes=tz_minute)),
    )


async def _after_connect_created(conn) -> None:
    conn.add_output_converter(SQL_SS_TIMESTAMPOFFSET, _decode_datetimeoffset)

# Full ODBC connection string. Build it once in the portal ("ADO.NET" or
# "ODBC" tab of your Azure SQL Database → Connection strings) and paste
# the whole thing here. Example:
#   Driver={ODBC Driver 18 for SQL Server};Server=tcp:<srv>.database.windows.net,1433;
#   Database=<db>;Uid=<user>;Pwd=<pwd>;Encrypt=yes;TrustServerCertificate=no;
#   Connection Timeout=30;
CONN_STR_ENV = "AZURE_SQL_CONNECTION_STRING"

# Pool sizing — the free serverless tier only supports a handful of concurrent
# connections; keep this small.
POOL_MIN = int(os.getenv("AZURE_SQL_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("AZURE_SQL_POOL_MAX", "4"))

_pool: aioodbc.Pool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> aioodbc.Pool:
    """Return the lazy singleton connection pool."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        dsn = os.getenv(CONN_STR_ENV)
        if not dsn:
            raise RuntimeError(f"{CONN_STR_ENV} must be set in the environment.")
        _pool = await aioodbc.create_pool(
            dsn=dsn,
            minsize=POOL_MIN,
            maxsize=POOL_MAX,
            autocommit=True,
            after_created=_after_connect_created,
        )
        return _pool


async def close_pool() -> None:
    """Close the pool (used in tests / graceful shutdown)."""
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


# ---- Query helpers ---------------------------------------------------------

def _row_to_dict(cursor, row) -> dict[str, Any]:
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


async def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run a non-returning statement. Returns affected row count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount


async def executemany(sql: str, seq_of_params: Sequence[Sequence[Any]]) -> int:
    """Batch insert/update. Returns total affected row count (driver-dependent)."""
    if not seq_of_params:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # fast_executemany massively speeds up bulk inserts on ODBC 18.
            try:
                cur.fast_executemany = True  # type: ignore[attr-defined]
            except AttributeError:
                pass
            await cur.executemany(sql, seq_of_params)
            return cur.rowcount if cur.rowcount is not None else len(seq_of_params)


async def fetch_one(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    """Return the first row as a dict, or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
            if row is None:
                return None
            return _row_to_dict(cur, row)


async def fetch_all(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Return all rows as list of dicts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]


# ---- Vector binding --------------------------------------------------------
#
# Azure SQL's VECTOR type accepts a JSON-array string cast with CAST(? AS VECTOR(n)).
# Callers should pass the JSON string as a parameter and use the CAST in their SQL,
# e.g.  INSERT ... VALUES (..., CAST(? AS VECTOR(768)))

def vector_literal(values: Sequence[float]) -> str:
    """Serialize an embedding to the JSON literal Azure SQL expects."""
    return json.dumps(list(values))
