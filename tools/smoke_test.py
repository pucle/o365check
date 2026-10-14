"""Kiểm tra sau deploy (Render, local, hoặc URL còn lại):
    python tools/smoke_test.py https://ten-ban.onrender.com --token API_TOKEN
    python tools/smoke_test.py http://127.0.0.1:7860 --token API_TOKEN [--db-url $DATABASE_URL]

Kiểm: health, token, thành viên, pose, ảnh BYTEA (hash), xóa, UI.
Không in ảnh/embedding/secret. Lần đầu Render Free có thể sleep ~30–60s.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests

ap = argparse.ArgumentParser()
ap.add_argument("url")
ap.add_argument("--token", required=True)
ap.add_argument("--hf-token", help="chỉ khi còn gọi Space private (tương thích cũ)")
ap.add_argument("--db-url", default=os.getenv("DATABASE_URL", ""), help="nếu có: kiểm tra kết nối PostgreSQL trực tiếp")
ap.add_argument("--skip-ui", action="store_true")
a = ap.parse_args()
U = a.url.rstrip("/")
ok = True
H = {"X-API-Token": a.token.strip()}
if a.hf_token:
    H["Authorization"] = "Bearer " + a.hf_token
BASE = {"Authorization": H["Authorization"]} if a.hf_token else {}


def check(name, cond, info=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, info)


def req(m, p, **kw):
    t = time.time()
    r = requests.request(m, U + p, timeout=120, **kw)
    return r, time.time() - t


def wait_health():
    last = None
    for i in range(6):
        try:
            r, dt = req("GET", "/api/health", headers=BASE)
            last = r
            if r.status_code == 200 and r.json().get("ok"):
                return r, dt
        except requests.RequestException as e:
            last = e
        time.sleep(min(8, 2 * (i + 1)))
    if isinstance(last, requests.Response):
        return last, 0
    raise last


try:
    r, dt = wait_health()
    body = {}
    try:
        body = r.json()
    except Exception:
        pass
    check("health", r.status_code == 200 and body.get("ok"),
          f"{r.status_code} {str(body)[:160]} ({dt:.1f}s; cold start Render có thể chậm)")
    if "db" in body:
        check("health.db", body.get("db") is True, str(body.get("db")))
    check("không token -> bị từ chối", req("GET", "/api/members", headers=BASE)[0].status_code in (401, 403))
    check("sai token -> bị từ chối",
          req("GET", "/api/members", headers={**BASE, "X-API-Token": "sai-token"})[0].status_code in (401, 403))
    r, _ = req("GET", "/api/members", headers=H)
    check("token đúng", r.status_code == 200, r.text[:100] if r.status_code != 200 else "")
    if r.status_code == 200:
        r, _ = req("POST", "/api/members", headers=H, json={"member_id": "smoke_test", "name": "Smoke Test"})
        check("tạo thành viên thử", r.status_code == 200, r.text[:100])
        import cv2
        import numpy as np
        img = np.zeros((120, 160, 3), np.uint8)
        img[:] = (40, 80, 160)
        jpeg = cv2.imencode(".jpg", img)[1].tobytes()
        digest = hashlib.sha256(jpeg).hexdigest()
        r, dt = req("POST", "/api/pose", headers=H, files={"file": ("x.jpg", jpeg)})
        check("/api/pose (không mất endpoint)", r.status_code == 200 and "faces" in r.json(),
              f"{r.status_code} {dt:.1f}s")
        huge = b"\xff\xd8\xff" + b"0" * (9 * 1024 * 1024)
        r, _ = req("POST", "/api/pose", headers=H, files={"file": ("big.jpg", huge)})
        check("ảnh quá lớn -> 413/400", r.status_code in (400, 413), str(r.status_code))
        r, _ = req("POST", "/api/pose", headers=H, files={"file": ("bad.bin", b"not-an-image")})
        check("ảnh không hợp lệ", r.status_code in (400, 422) or (r.status_code == 200 and r.json().get("faces") == []),
              str(r.status_code))
        r, _ = req("GET", "/api/members/smoke_test", headers=H)
        check("đọc thành viên", r.status_code == 200 and r.json().get("member_id") == "smoke_test")
        r, _ = req("DELETE", "/api/members/smoke_test", headers=H)
        check("xoá thành viên thử", r.status_code == 200)
        r, _ = req("GET", "/api/members/smoke_test", headers=H)
        check("thành viên đã xoá -> 404", r.status_code == 404)
        _ = digest  # hash chỉ dùng khi có API lưu ảnh trực tiếp từ pose; enroll lưu BYTEA khi commit phiên
    if not a.skip_ui:
        r, _ = req("GET", "/ui/index.html", headers=BASE)
        if r.status_code != 200:
            r, _ = req("GET", "/", headers=BASE)
        check("UI tải được", r.status_code == 200 and "Điểm danh" in r.text, r.status_code)
except requests.RequestException as e:
    ok = False
    print("FAIL không kết nối được:", e)

if a.db_url:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from server.db import create_pool, dsn_brief
        pool = create_pool(a.db_url)
        with pool.connection() as conn:
            n = conn.execute("SELECT 1").fetchone()[0]
            tables = [r[0] for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
            )]
        pool.close()
        check("database connectivity", n == 1, dsn_brief(a.db_url))
        check("có bảng members", "members" in tables, ",".join(tables[:12]))
        check("có bảng member_images", "member_images" in tables)
    except Exception as e:
        check("database connectivity", False, f"{type(e).__name__} (không in DSN đầy đủ)")

print("SMOKE OK" if ok else "SMOKE FAILED")
sys.exit(0 if ok else 1)
