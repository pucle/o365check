"""Lớp lưu trữ PostgreSQL (Neon). Giữ ngữ nghĩa API cũ của SQLite Storage."""
from __future__ import annotations

import json
import logging
import threading
import time
import zipfile
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from server.db import apply_migrations, as_bytes, create_pool, dsn_brief

log = logging.getLogger("club.storage")


class Storage:
    def __init__(self, dsn: str, migrate: bool = True):
        self.dsn = dsn
        self.pool = create_pool(dsn)
        self.lock = threading.RLock()
        if migrate:
            apply_migrations(self.pool)

    def close(self) -> None:
        try:
            self.pool.close()
        except Exception:
            pass

    def ping(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT 1")

    @contextmanager
    def connection(self):
        try:
            self.pool.check()
        except Exception:
            pass
        with self.pool.connection() as conn:
            yield conn

    def execute(self, sql: str, args: Iterable = ()) -> int:
        with self.lock:
            with self.connection() as conn:
                cur = conn.execute(sql, tuple(args))
                if cur.description and cur.description[0].name == "id":
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
                return cur.rowcount or 0

    def query(self, sql: str, args: Iterable = ()) -> List[tuple]:
        with self.lock:
            with self.connection() as conn:
                return list(conn.execute(sql, tuple(args)))

    # ---- kv / cấu hình ------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.query("SELECT value FROM kv WHERE key=%s", (key,))
        if not r:
            return default
        try:
            return json.loads(r[0][0])
        except (ValueError, TypeError):
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (key, json.dumps(value)),
        )

    # ---- thành viên ---------------------------------------------------
    def upsert_member(self, member_id: str, name: str, created: str) -> None:
        self.execute(
            "INSERT INTO members(id,name,created) VALUES(%s,%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name",
            (member_id, name, created),
        )

    def members(self) -> List[Tuple[str, str]]:
        return [(r[0], r[1]) for r in self.query("SELECT id,name FROM members ORDER BY lower(name)")]

    def member_name(self, member_id: Optional[str]) -> str:
        if not member_id:
            return ""
        r = self.query("SELECT name FROM members WHERE id=%s", (member_id,))
        return r[0][0] if r else member_id

    def member_exists(self, member_id: str) -> bool:
        return bool(self.query("SELECT 1 FROM members WHERE id=%s", (member_id,)))

    def delete_member(self, member_id: str, keep_shifts: bool = False) -> None:
        with self.lock:
            with self.connection() as conn:
                conn.execute("DELETE FROM embeddings WHERE member_id=%s", (member_id,))
                conn.execute("DELETE FROM member_images WHERE member_id=%s", (member_id,))
                conn.execute("DELETE FROM members WHERE id=%s", (member_id,))
                if not keep_shifts:
                    conn.execute("DELETE FROM shifts WHERE member_id=%s", (member_id,))

    # ---- ảnh BYTEA ----------------------------------------------------
    def add_member_image(self, member_id: str, img_id: str, data: bytes, mime: str, created: str) -> None:
        if not data:
            return
        self.execute(
            "INSERT INTO member_images(member_id,img_id,mime,byte_size,created,data) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (member_id, img_id) DO UPDATE SET mime=EXCLUDED.mime, byte_size=EXCLUDED.byte_size, "
            "created=EXCLUDED.created, data=EXCLUDED.data",
            (member_id, img_id, mime, len(data), created, data),
        )

    def add_member_images(self, conn, member_id: str, images: Dict[str, Tuple[bytes, str]], created: str) -> int:
        n = 0
        for img_id, (data, mime) in images.items():
            if not data:
                continue
            conn.execute(
                "INSERT INTO member_images(member_id,img_id,mime,byte_size,created,data) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (member_id, img_id) DO UPDATE SET mime=EXCLUDED.mime, byte_size=EXCLUDED.byte_size, "
                "created=EXCLUDED.created, data=EXCLUDED.data",
                (member_id, img_id, mime, len(data), created, data),
            )
            n += 1
        return n

    def get_member_image(self, member_id: str, img_id: str) -> Optional[Tuple[bytes, str, int]]:
        r = self.query(
            "SELECT data,mime,byte_size FROM member_images WHERE member_id=%s AND img_id=%s",
            (member_id, img_id),
        )
        if not r:
            return None
        return as_bytes(r[0][0]), r[0][1], int(r[0][2])

    def image_counts(self) -> Dict[str, int]:
        return {m: int(n) for m, n in self.query("SELECT member_id, COUNT(*) FROM member_images GROUP BY member_id")}

    def delete_images_by_img(self, conn, member_id: str, imgs: List[str]) -> int:
        if not imgs:
            return 0
        q = ",".join(["%s"] * len(imgs))
        cur = conn.execute(
            f"DELETE FROM member_images WHERE member_id=%s AND img_id IN ({q})",
            [member_id, *imgs],
        )
        return cur.rowcount or 0

    # ---- embedding ----------------------------------------------------
    def add_embedding(self, member_id: str, vec: np.ndarray, source: str, pitch, yaw,
                      created: str, img: str, step: str = "") -> None:
        self.execute(
            "INSERT INTO embeddings(member_id,vec,source,pitch,yaw,created,img,step) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (member_id, np.asarray(vec, dtype=np.float32).tobytes(), source, pitch, yaw, created, img, step),
        )

    def add_embeddings_bulk(self, rows: List[tuple]) -> None:
        data = [(m, np.asarray(v, dtype=np.float32).tobytes(), src, p, y, c, i, st) for m, v, src, p, y, c, i, st in rows]
        with self.lock:
            with self.connection() as conn:
                conn.executemany(
                    "INSERT INTO embeddings(member_id,vec,source,pitch,yaw,created,img,step) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    data,
                )

    def commit_enrollment(self, member_id: str, name: str, created: str, upsert_member: bool,
                          replace: bool, rows: List[tuple], images: Dict[str, Tuple[bytes, str]]) -> None:
        """Một giao dịch: thành viên + ảnh BYTEA + embeddings. Lỗi → rollback toàn bộ."""
        with self.lock:
            with self.connection() as conn:
                if upsert_member:
                    conn.execute(
                        "INSERT INTO members(id,name,created) VALUES(%s,%s,%s) "
                        "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name",
                        (member_id, name, created),
                    )
                if replace:
                    conn.execute("DELETE FROM embeddings WHERE member_id=%s", (member_id,))
                    conn.execute("DELETE FROM member_images WHERE member_id=%s", (member_id,))
                self.add_member_images(conn, member_id, images, created)
                data = [(m, np.asarray(v, dtype=np.float32).tobytes(), src, p, y, c, i, st)
                        for m, v, src, p, y, c, i, st in rows]
                if data:
                    conn.executemany(
                        "INSERT INTO embeddings(member_id,vec,source,pitch,yaw,created,img,step) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        data,
                    )

    def add_sample_with_image(self, member_id: str, img_id: str, jpeg: bytes, mime: str, created: str,
                              variants: List[tuple]) -> None:
        """variants: (vec, source, pitch, yaw, step)."""
        with self.lock:
            with self.connection() as conn:
                if jpeg:
                    conn.execute(
                        "INSERT INTO member_images(member_id,img_id,mime,byte_size,created,data) VALUES(%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (member_id, img_id) DO UPDATE SET mime=EXCLUDED.mime, byte_size=EXCLUDED.byte_size, "
                        "created=EXCLUDED.created, data=EXCLUDED.data",
                        (member_id, img_id, mime, len(jpeg), created, jpeg),
                    )
                data = [(member_id, np.asarray(v, dtype=np.float32).tobytes(), src, p, y, created, img_id, st)
                        for v, src, p, y, st in variants]
                conn.executemany(
                    "INSERT INTO embeddings(member_id,vec,source,pitch,yaw,created,img,step) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    data,
                )

    def delete_embeddings_by_img(self, member_id: str, imgs: Iterable[str]) -> int:
        imgs = list(imgs)
        if not imgs:
            return 0
        q = ",".join(["%s"] * len(imgs))
        with self.lock:
            with self.connection() as conn:
                cur = conn.execute(
                    f"DELETE FROM embeddings WHERE member_id=%s AND img IN ({q})",
                    [member_id, *imgs],
                )
                n = cur.rowcount or 0
                self.delete_images_by_img(conn, member_id, imgs)
                return n

    def all_embeddings(self) -> List[dict]:
        rows = self.query(
            "SELECT member_id,vec,source,pitch,yaw,img,id,step,created FROM embeddings ORDER BY id"
        )
        out = []
        for m, b, s, p, y, i, k, st, c in rows:
            out.append({
                "member_id": m,
                "vec": np.frombuffer(as_bytes(b), dtype=np.float32).copy(),
                "source": s, "pitch": p, "yaw": y,
                "img": (i if i else f"legacy-{k}"),
                "id": k, "step": st, "created": c,
            })
        return out

    def delete_embeddings(self, member_id: str, source: Optional[str] = None) -> int:
        with self.lock:
            with self.connection() as conn:
                if source:
                    cur = conn.execute("DELETE FROM embeddings WHERE member_id=%s AND source=%s", (member_id, source))
                else:
                    cur = conn.execute("DELETE FROM embeddings WHERE member_id=%s", (member_id,))
                    conn.execute("DELETE FROM member_images WHERE member_id=%s", (member_id,))
                return cur.rowcount or 0

    # ---- mẫu người lạ -------------------------------------------------
    def add_stranger(self, vec: np.ndarray, pitch, yaw, context: str, created: str) -> None:
        self.execute(
            "INSERT INTO strangers(vec,pitch,yaw,context,created) VALUES(%s,%s,%s,%s,%s)",
            (np.asarray(vec, dtype=np.float32).tobytes(), pitch, yaw, context, created),
        )

    def strangers(self) -> np.ndarray:
        rows = self.query("SELECT vec FROM strangers ORDER BY id")
        if not rows:
            return np.zeros((0, 512), dtype=np.float32)
        return np.stack([np.frombuffer(as_bytes(r[0]), dtype=np.float32) for r in rows])

    def clear_strangers(self) -> int:
        return self.execute("DELETE FROM strangers")

    # ---- ca trực ------------------------------------------------------
    def add_shift(self, member_id: str, start: str, end: str) -> int:
        with self.lock:
            with self.connection() as conn:
                row = conn.execute(
                    "INSERT INTO shifts(member_id,start,\"end\") VALUES(%s,%s,%s) RETURNING id",
                    (member_id, start, end),
                ).fetchone()
                return int(row[0])

    def get_shift(self, shift_id: int) -> Optional[tuple]:
        r = self.query('SELECT id,member_id,start,"end" FROM shifts WHERE id=%s', (shift_id,))
        return r[0] if r else None

    def delete_shift(self, shift_id: int) -> None:
        self.execute("DELETE FROM shifts WHERE id=%s", (shift_id,))

    def shifts_between(self, lo: str, hi: str) -> List[tuple]:
        return self.query(
            'SELECT id,member_id,start,"end" FROM shifts WHERE start>=%s AND start<%s ORDER BY start',
            (lo, hi),
        )

    def all_shifts(self) -> List[tuple]:
        return self.query('SELECT id,member_id,start,"end" FROM shifts')

    # ---- sự kiện / vắng mặt -------------------------------------------
    def add_event(self, ts: str, kind: str, member_id: Optional[str], detail: str = "") -> None:
        self.execute(
            "INSERT INTO events(ts,kind,member_id,detail) VALUES(%s,%s,%s,%s)",
            (ts, kind, member_id, detail),
        )

    def events_between(self, lo: str, hi: str) -> List[tuple]:
        return self.query(
            "SELECT ts,kind,member_id,detail FROM events WHERE ts>=%s AND ts<%s ORDER BY ts",
            (lo, hi),
        )

    def recent_events(self, n: int = 30) -> List[tuple]:
        return self.query("SELECT ts,kind,member_id,detail FROM events ORDER BY id DESC LIMIT %s", (n,))

    def add_absence(self, member_id: str, start: str, end: str, minutes: float) -> None:
        self.execute(
            "INSERT INTO absences(member_id,start,\"end\",minutes) VALUES(%s,%s,%s,%s)",
            (member_id, start, end, minutes),
        )

    def absences_for(self, member_id: str, lo: str, hi: str) -> List[tuple]:
        return self.query(
            'SELECT start,"end",minutes FROM absences WHERE member_id=%s AND start>=%s AND "end"<=%s ORDER BY start',
            (member_id, lo, hi),
        )

    def table_counts(self) -> Dict[str, int]:
        names = ["members", "embeddings", "member_images", "strangers", "shifts", "events", "absences", "kv",
                 "enroll_sessions"]
        out = {}
        for n in names:
            try:
                out[n] = int(self.query(f"SELECT COUNT(*) FROM {n}")[0][0])
            except Exception:
                out[n] = -1
        return out

    # ---- phiên đăng ký ------------------------------------------------
    def save_enroll_session(self, sess) -> None:
        rejects = json.dumps(dict(sess.rejects))
        pending = sess.pending
        report = json.dumps(pending["report"]) if pending else None
        replace = bool(pending["replace"]) if pending else None
        with self.lock:
            with self.connection() as conn:
                conn.execute(
                    "INSERT INTO enroll_sessions(sid,member_id,name,mode,kind,source,created,touched,seq,frames,rejects,"
                    "pending_report,pending_replace) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(sid) DO UPDATE SET member_id=EXCLUDED.member_id, name=EXCLUDED.name, mode=EXCLUDED.mode, "
                    "kind=EXCLUDED.kind, source=EXCLUDED.source, created=EXCLUDED.created, touched=EXCLUDED.touched, "
                    "seq=EXCLUDED.seq, frames=EXCLUDED.frames, rejects=EXCLUDED.rejects, "
                    "pending_report=EXCLUDED.pending_report, pending_replace=EXCLUDED.pending_replace",
                    (sess.sid, sess.member_id, sess.name, sess.mode, sess.kind, sess.source, sess.created, sess.touched,
                     sess.seq, sess.frames, rejects, report, replace),
                )
                conn.execute("DELETE FROM enroll_candidates WHERE sid=%s", (sess.sid,))
                rows = []
                for c in sess.all():
                    kps = c.get("kps")
                    kps_b = None if kps is None else np.asarray(kps, dtype=np.float32).tobytes()
                    emb = c.get("emb")
                    emb_b = None if emb is None else np.asarray(emb, dtype=np.float32).tobytes()
                    rows.append((
                        sess.sid, int(c["fid"]), c.get("dir"), c.get("pitch"), c.get("yaw"), c.get("det"),
                        c.get("width"), c.get("blur"), c.get("light"), bool(c.get("clipped")), c.get("nfaces"),
                        emb_b, kps_b, json.dumps(c.get("bbox")), c.get("jpg") or None,
                    ))
                if rows:
                    conn.executemany(
                        "INSERT INTO enroll_candidates(sid,fid,dir,pitch,yaw,det,width,blur,light,clipped,nfaces,emb,kps,bbox,jpg) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        rows,
                    )
                conn.execute("DELETE FROM enroll_pending_rows WHERE sid=%s", (sess.sid,))
                if pending and pending.get("rows"):
                    prow = []
                    for i, r in enumerate(pending["rows"]):
                        prow.append((
                            sess.sid, i, np.asarray(r["emb"], dtype=np.float32).tobytes(),
                            r.get("pitch"), r.get("yaw"), r.get("img"), r.get("step"),
                        ))
                    conn.executemany(
                        "INSERT INTO enroll_pending_rows(sid,ord,emb,pitch,yaw,img,step) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        prow,
                    )

    def load_enroll_session(self, sid: str):
        from collections import Counter
        from server import enrolling

        rows = self.query(
            "SELECT sid,member_id,name,mode,kind,source,created,touched,seq,frames,rejects,pending_report,pending_replace "
            "FROM enroll_sessions WHERE sid=%s",
            (sid,),
        )
        if not rows:
            return None
        r = rows[0]
        sess = enrolling.Session(r[1], r[2] or "", r[3], r[4], r[5])
        sess.sid = r[0]
        sess.created, sess.touched = float(r[6]), float(r[7])
        sess.seq, sess.frames = int(r[8]), int(r[9])
        try:
            sess.rejects = Counter(json.loads(r[10] or "{}"))
        except (ValueError, TypeError):
            sess.rejects = Counter()
        crows = self.query(
            "SELECT fid,dir,pitch,yaw,det,width,blur,light,clipped,nfaces,emb,kps,bbox,jpg "
            "FROM enroll_candidates WHERE sid=%s ORDER BY fid",
            (sid,),
        )
        for fid, d, pitch, yaw, det, width, blur, light, clipped, nfaces, emb, kps, bbox, jpg in crows:
            cand = dict(
                fid=int(fid), dir=d, pitch=pitch, yaw=yaw, det=det, width=width, blur=blur, light=light,
                clipped=bool(clipped), nfaces=nfaces,
                emb=None if emb is None else np.frombuffer(as_bytes(emb), dtype=np.float32).copy(),
                kps=None if kps is None else np.frombuffer(as_bytes(kps), dtype=np.float32).copy().reshape(-1, 2),
                bbox=tuple(json.loads(bbox)) if bbox else None,
                jpg=as_bytes(jpg) if jpg is not None else b"",
            )
            bucket = sess.pool[d if d in sess.pool else None]
            bucket.append(cand)
        if r[11]:
            try:
                report = json.loads(r[11])
            except (ValueError, TypeError):
                report = None
            if report is not None:
                prow = self.query(
                    "SELECT ord,emb,pitch,yaw,img,step FROM enroll_pending_rows WHERE sid=%s ORDER BY ord",
                    (sid,),
                )
                plan_rows = [{
                    "emb": np.frombuffer(as_bytes(emb), dtype=np.float32).copy(),
                    "pitch": p, "yaw": y, "img": img, "step": st,
                } for _ord, emb, p, y, img, st in prow]
                sess.pending = {"report": report, "rows": plan_rows, "replace": bool(r[12])}
        return sess

    def delete_enroll_session(self, sid: str) -> bool:
        n = self.execute("DELETE FROM enroll_sessions WHERE sid=%s", (sid,))
        return n > 0

    def sweep_enroll_sessions(self, ttl: float, now: Optional[float] = None, keep: Optional[Iterable[str]] = None) -> int:
        now = now or time.time()
        keep = set(keep or [])
        rows = self.query("SELECT sid FROM enroll_sessions WHERE touched < %s", (now - ttl,))
        n = 0
        for (sid,) in rows:
            if sid in keep:
                continue
            if self.delete_enroll_session(sid):
                n += 1
        return n

    def count_enroll_sessions(self) -> int:
        return int(self.query("SELECT COUNT(*) FROM enroll_sessions")[0][0])

    # ---- sao lưu (không ghi embedding/ảnh ra log) ---------------------
    def backup_to(self, dest_path: str) -> None:
        """Gói JSON + ảnh JPEG vào zip. Không phải file SQLite cũ."""
        meta = {
            "format": "club-backup-v2",
            "members": [dict(id=i, name=n, created=c) for i, n, c in
                        self.query("SELECT id,name,created FROM members")],
            "shifts": [dict(id=i, member_id=m, start=s, end=e) for i, m, s, e in self.all_shifts()],
            "events": [dict(ts=t, kind=k, member_id=m, detail=d) for t, k, m, d in
                       self.query("SELECT ts,kind,member_id,detail FROM events ORDER BY id")],
            "absences": [dict(member_id=m, start=s, end=e, minutes=mins) for m, s, e, mins in
                         self.query('SELECT member_id,start,"end",minutes FROM absences')],
            "kv": {k: v for k, v in self.query("SELECT key,value FROM kv")},
            "embeddings": [],
            "image_index": [],
        }
        embs = self.query("SELECT id,member_id,source,pitch,yaw,created,img,step,vec FROM embeddings ORDER BY id")
        img_rows = self.query("SELECT member_id,img_id,mime,byte_size,created,data FROM member_images")
        with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for i, m, src, p, y, c, img, st, vec in embs:
                name = f"embeddings/{i}.f32"
                z.writestr(name, as_bytes(vec))
                meta["embeddings"].append({
                    "id": i, "member_id": m, "source": src, "pitch": p, "yaw": y,
                    "created": c, "img": img, "step": st, "file": name,
                })
            for m, img_id, mime, size, c, data in img_rows:
                ext = "jpg" if (mime or "").endswith("jpeg") else "bin"
                path = f"images/{m}/{img_id}.{ext}"
                z.writestr(path, as_bytes(data))
                meta["image_index"].append({
                    "member_id": m, "img_id": img_id, "mime": mime, "byte_size": size, "created": c, "file": path,
                })
            z.writestr("meta.json", json.dumps(meta, ensure_ascii=False))

    def brief(self) -> str:
        return dsn_brief(self.dsn)
