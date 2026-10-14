"""Kết nối Neon/PostgreSQL: pool nhỏ, reconnect, migration có phiên bản. Không in secret."""
from __future__ import annotations

import logging
import os
import time
from typing import List
from urllib.parse import urlparse

from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("club.db")

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def normalize_dsn(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def dsn_brief(url: str) -> str:
    try:
        p = urlparse(normalize_dsn(url))
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        db = p.path or ""
        return f"{p.scheme}://{host}{port}{db}"
    except Exception:
        return "(unparseable)"


def create_pool(dsn: str, min_size: int = 1, max_size: int = 4) -> ConnectionPool:
    dsn = normalize_dsn(dsn)
    max_size = max(1, min(int(os.getenv("DB_POOL_MAX", max_size)), 8))
    kwargs = {
        "row_factory": tuple_row,
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        "prepare_threshold": 0,  # Neon transaction pooling (PgBouncer)
        "options": "-c timezone=UTC",
    }
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=30,
        kwargs=kwargs,
        open=False,
        reconnect_timeout=60,
    )
    last = None
    for attempt in range(1, 4):
        try:
            pool.open()
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            log.info("PostgreSQL sẵn sàng (%s), pool %d–%d", dsn_brief(dsn), min_size, max_size)
            return pool
        except Exception as e:
            last = e
            log.warning("Kết nối database lần %d thất bại (%s): %s", attempt, dsn_brief(dsn), type(e).__name__)
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Không kết nối được PostgreSQL tại {dsn_brief(dsn)}: {type(last).__name__}") from last


def apply_migrations(pool: ConnectionPool, directory: Optional[str] = None) -> List[str]:
    directory = directory or MIGRATIONS_DIR
    files = sorted(f for f in os.listdir(directory) if f.endswith(".sql") and f[:3].isdigit())
    applied_now: List[str] = []
    with pool.connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                 version TEXT PRIMARY KEY,
                 applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
               )"""
        )
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        have = {r[0] for r in rows}
        for name in files:
            ver = name.split("_", 1)[0]
            if ver in have:
                continue
            path = os.path.join(directory, name)
            sql = open(path, encoding="utf-8").read()
            for stmt in _sql_statements(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (ver,))
            applied_now.append(name)
            log.info("Đã chạy migration %s", name)
    return applied_now


def _sql_statements(sql: str) -> List[str]:
    parts, buf = [], []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def as_bytes(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytearray):
        return bytes(value)
    return bytes(value)
