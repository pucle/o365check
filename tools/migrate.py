"""Chạy migration PostgreSQL (idempotent, không DROP bảng).
    python tools/migrate.py
Cần DATABASE_URL. Không in connection string."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from server.db import apply_migrations, create_pool, dsn_brief  # noqa: E402


def main() -> int:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        print("Thiếu DATABASE_URL", file=sys.stderr)
        return 2
    print("Chạy migration trên", dsn_brief(dsn))
    pool = create_pool(dsn)
    try:
        done = apply_migrations(pool)
        if done:
            print("Đã áp dụng:", ", ".join(done))
        else:
            print("Không có migration mới.")
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        print("Phiên bản đã có:")
        for v, t in rows:
            print(" ", v, t)
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
