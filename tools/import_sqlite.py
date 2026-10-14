"""Nhập club.db SQLite cũ sang Neon PostgreSQL. Không xóa file SQLite. Không in ảnh/embedding.

    python tools/import_sqlite.py [--db data/club.db] [--dry-run]

Idempotent theo khóa chính: bản ghi đã có thì bỏ qua. Cần DATABASE_URL.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from server.db import apply_migrations, as_bytes, create_pool, dsn_brief  # noqa: E402

TABLES = ("members", "embeddings", "strangers", "shifts", "events", "absences", "kv")


def sqlite_cols(db, table: str) -> set:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def sqlite_counts(path: str) -> dict:
    db = sqlite3.connect(path)
    out = {}
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in TABLES:
        if t not in names:
            out[t] = None
            continue
        out[t] = int(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    db.close()
    return out


def pg_counts(conn) -> dict:
    out = {}
    for t in TABLES + ("member_images",):
        try:
            out[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        except Exception:
            out[t] = -1
    return out


def insert_row(conn, table: str, cols: str, placeholders: str, vals) -> str:
    cur = conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
        vals,
    )
    return "inserted" if cur.rowcount else "skipped"


def copy_rows(conn, rows_vals, table: str, cols: str, placeholders: str) -> tuple:
    inserted = skipped = errors = 0
    for vals in rows_vals:
        try:
            kind = insert_row(conn, table, cols, placeholders, vals)
            if kind == "inserted":
                inserted += 1
            else:
                skipped += 1
        except Exception:
            errors += 1
    return inserted, skipped, errors


def reset_seq(conn, table: str) -> None:
    conn.execute(
        "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
        "GREATEST((SELECT COALESCE(MAX(id), 1) FROM " + table + "), 1))",
        (table,),
    )


def iter_members(src):
    cols = sqlite_cols(src, "members")
    sel = "id,name" + (",created" if "created" in cols else "")
    for r in src.execute(f"SELECT {sel} FROM members"):
        d = dict(r)
        yield (d["id"], d["name"], d.get("created"))


def iter_embeddings(src):
    cols = sqlite_cols(src, "embeddings")
    fields = [c for c in ("id", "member_id", "vec", "source", "pitch", "yaw", "created", "img", "step") if c in cols]
    for r in src.execute(f"SELECT {','.join(fields)} FROM embeddings"):
        d = dict(r)
        yield (
            d["id"], d["member_id"], as_bytes(d["vec"]), d.get("source"), d.get("pitch"), d.get("yaw"),
            d.get("created"), d.get("img") or "", d.get("step") or "",
        )


def iter_strangers(src):
    for r in src.execute("SELECT id,vec,pitch,yaw,context,created FROM strangers"):
        yield (r["id"], as_bytes(r["vec"]), r["pitch"], r["yaw"], r["context"], r["created"])


def iter_simple(src, sql):
    for r in src.execute(sql):
        yield tuple(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "club.db"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        print("Thiếu DATABASE_URL", file=sys.stderr)
        return 2
    if not os.path.isfile(a.db):
        print(f"Không tìm thấy SQLite: {a.db}")
        print("Không có dữ liệu cũ để nhập. Không tạo bản ghi giả.")
        return 3
    sc = sqlite_counts(a.db)
    print("SQLite", a.db)
    for t, n in sc.items():
        print(f"  {t}: {n if n is not None else '(không có bảng)'}")
    print("Đích PostgreSQL", dsn_brief(dsn), "(dry-run)" if a.dry_run else "")
    if a.dry_run:
        return 0
    pool = create_pool(dsn)
    try:
        apply_migrations(pool)
        src = sqlite3.connect(a.db)
        src.row_factory = sqlite3.Row
        names = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with pool.connection() as conn:
            before = pg_counts(conn)
            stats = {}
            if "members" in names:
                stats["members"] = copy_rows(conn, iter_members(src), "members", "id,name,created", "%s,%s,%s")
            if "embeddings" in names:
                stats["embeddings"] = copy_rows(
                    conn, iter_embeddings(src), "embeddings",
                    "id,member_id,vec,source,pitch,yaw,created,img,step",
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s",
                )
            if "strangers" in names:
                stats["strangers"] = copy_rows(
                    conn, iter_strangers(src), "strangers",
                    "id,vec,pitch,yaw,context,created", "%s,%s,%s,%s,%s,%s",
                )
            if "shifts" in names:
                stats["shifts"] = copy_rows(
                    conn, iter_simple(src, "SELECT id,member_id,start,end FROM shifts"),
                    "shifts", 'id,member_id,start,"end"', "%s,%s,%s,%s",
                )
            if "events" in names:
                stats["events"] = copy_rows(
                    conn, iter_simple(src, "SELECT id,ts,kind,member_id,detail FROM events"),
                    "events", "id,ts,kind,member_id,detail", "%s,%s,%s,%s,%s",
                )
            if "absences" in names:
                stats["absences"] = copy_rows(
                    conn, iter_simple(src, "SELECT id,member_id,start,end,minutes FROM absences"),
                    "absences", 'id,member_id,start,"end",minutes', "%s,%s,%s,%s,%s",
                )
            if "kv" in names:
                stats["kv"] = copy_rows(
                    conn, iter_simple(src, "SELECT key,value FROM kv"), "kv", "key,value", "%s,%s",
                )
            for t in ("embeddings", "strangers", "shifts", "events", "absences"):
                try:
                    reset_seq(conn, t)
                except Exception:
                    pass
            after = pg_counts(conn)
        src.close()
        print("Kết quả (inserted, skipped, errors):")
        for t, st in stats.items():
            print(f"  {t}: {st}  sqlite={sc.get(t)}  pg trước={before.get(t)} sau={after.get(t)}")
        print("SQLite gốc được giữ nguyên.")
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
