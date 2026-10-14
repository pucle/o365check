"""Server chấm công khuôn mặt cho CLB.  Chạy:  uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-7860}

Giao diện web: http://localhost:7860/ui/
Biến môi trường:
  API_TOKEN       (bắt buộc) mật khẩu API/giao diện; để trống thì mọi yêu cầu bị từ chối
  DATABASE_URL    (bắt buộc) chuỗi kết nối Neon/PostgreSQL
  PORT            cổng HTTP (Render cung cấp; mặc định 7860 khi chạy local)
  MODEL_NAME      buffalo_l (mặc định, chính xác hơn) hoặc buffalo_s (nhanh hơn)
  TZ_NAME         múi giờ, mặc định Asia/Ho_Chi_Minh
  CORS_ORIGINS    origin UI nếu khác domain backend (phẩy)
  APP_ENV         production trên Render: từ chối API_TOKEN yếu (dev)
  INSIGHTFACE_HOME  nơi lưu model (mặc định ~/.insightface)
Ngưỡng và thời gian chỉnh trong giao diện (trang Hiệu chỉnh), xem common/config.py.
"""
from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import os
import sys
import tempfile
import threading
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import calibration, config, posebins  # noqa: E402
from common.matching import Gallery  # noqa: E402
from common.presence import AWAY, PRESENT, Absence, PresenceEngine, Shift  # noqa: E402
from server import enrolling  # noqa: E402
from server.db import dsn_brief  # noqa: E402
from server.face_engine import Face, FaceEngine, decode_image  # noqa: E402
from server.images import ImageError, MAX_UPLOAD_BYTES, decode_checked, to_jpeg  # noqa: E402
from server.storage import Storage  # noqa: E402

log = logging.getLogger("club")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

try:
    TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Ho_Chi_Minh"))
except Exception:                      # Windows thiếu gói tzdata -> dùng cố định UTC+7
    TZ = timezone(timedelta(hours=7))

# Windows `set API_TOKEN=abc ` giữ lại khoảng trắng/dấu nháy ở cuối -> bỏ đi để khỏi lệch
API_TOKEN = os.getenv("API_TOKEN", "").strip().strip("\"'").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip().strip("\"'")
APP_ENV = os.getenv("APP_ENV", os.getenv("RENDER", "") and "production" or "local").strip().lower()
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ID_RE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
WEAK_TOKENS = frozenset({"dev", "changeme", "secret", "password", "token", "api_token"})

# Cổng chất lượng khi đăng ký: nguồn "door" yêu cầu mặt rõ, "room" chấp nhận mặt nhỏ/nhìn từ trên cao
ENROLL_GATES = {
    "door": dict(min_det=0.60, min_w=80, max_yaw=60.0, max_pitch=50.0, min_blur=35.0),
    "room": dict(min_det=0.45, min_w=40, max_yaw=85.0, max_pitch=75.0, min_blur=15.0),
}
MAX_IMAGES_PER_MEMBER = 150
HANDOFF_TTL_S = 60
DOOR_MIN_W, DOOR_MAX_YAW, DOOR_MAX_PITCH = 80, 40.0, 30.0


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)          # giờ địa phương, không kèm tzinfo


def to_local_naive(dt: datetime) -> datetime:
    return dt.astimezone(TZ).replace(tzinfo=None) if dt.tzinfo else dt


class Ctx:
    engine: Optional[FaceEngine] = None
    engine_status: str = "pending"   # pending | loading | ready | failed
    engine_error: str = ""
    store: Storage
    gallery: Gallery
    presence: PresenceEngine
    settings: Dict[str, Any]
    glock = threading.RLock()                 # bảo vệ gallery khi thêm/xoá mẫu
    plock = threading.RLock()                 # bảo vệ presence engine
    handoff: Dict[str, datetime] = {}         # member_id -> lúc check-in ở cửa, chờ cam trần gán track
    room: Dict[str, Any] = {"persons": 0, "strangers": 0, "ts": None, "identified": []}
    frame_votes: Dict[str, List[datetime]] = {}
    started: Optional[datetime] = None
    sessions = enrolling.SessionStore()      # phiên đăng ký (RAM + PostgreSQL, có TTL)


ctx = Ctx()


# --------------------------------------------------------------------- tiện ích
def auth(x_api_token: str = Header(default="")) -> None:
    # Header riêng X-API-Token để header Authorization còn trống cho token Hugging Face (Space Private).
    # Fail-closed: chưa đặt API_TOKEN thì từ chối tất cả.
    if not API_TOKEN:
        raise HTTPException(401, "Server chưa đặt biến môi trường API_TOKEN (đặt rồi chạy lại uvicorn).")
    if not x_api_token.strip():
        raise HTTPException(401, "Trình duyệt chưa gửi token: bấm nút Token và nhập API_TOKEN.")
    if not hmac.compare_digest(x_api_token.strip().encode("utf-8"), API_TOKEN.encode("utf-8")):
        raise HTTPException(401, "Token không khớp API_TOKEN của server (kiểm tra khoảng trắng/dấu nháy).")


def S(key: str):
    return ctx.settings[key]


def default_model_name() -> str:
    explicit = os.getenv("MODEL_NAME", "").strip()
    if explicit:
        return explicit
    if os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"):
        return "buffalo_s"
    return "buffalo_l"


def skip_model_warmup() -> bool:
    v = os.getenv("SKIP_MODEL_WARMUP", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    return bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID"))


def get_engine() -> FaceEngine:
    if ctx.engine is not None:
        return ctx.engine
    if ctx.engine_status == "failed":
        raise HTTPException(503, "Không tải được model nhận diện (thường do hết RAM trên Render Free). "
                                 "Đặt MODEL_NAME=buffalo_s và SKIP_MODEL_WARMUP=1 rồi deploy lại.")
    raise HTTPException(503, "Model đang tải, thử lại sau ~30–60 giây (cold start Render).")


def require_env() -> None:
    missing = []
    if not API_TOKEN:
        missing.append("API_TOKEN")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if missing:
        raise RuntimeError("Thiếu biến môi trường bắt buộc: " + ", ".join(missing)
                           + ". Đặt trong Render Environment / file .env local. Không in giá trị secret.")
    if APP_ENV in ("production", "prod") and API_TOKEN.lower() in WEAK_TOKENS:
        raise RuntimeError("API_TOKEN quá yếu cho môi trường production (không dùng 'dev').")


def read_upload_bytes(up: UploadFile) -> bytes:
    data = up.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Ảnh quá lớn (tối đa 8 MB).")
    return data


def read_image(up: UploadFile) -> Optional[np.ndarray]:
    try:
        data = read_upload_bytes(up)
        if not data:
            return None
        try:
            return decode_checked(data)
        except ImageError:
            return decode_image(data)
    except HTTPException:
        raise
    except Exception:
        return None


def jpeg_of(img: np.ndarray) -> bytes:
    try:
        return to_jpeg(img)[0]
    except ImageError:
        return b""


def check_member_id(member_id: str) -> str:
    member_id = (member_id or "").strip()
    if not member_id or len(member_id) > 40 or not member_id.strip(".") or any(c not in ID_RE_CHARS for c in member_id):
        raise HTTPException(400, "Mã thành viên chỉ gồm chữ không dấu, số, '-', '_' hoặc '.', tối đa 40 ký tự.")
    return member_id


def load_gallery() -> None:
    rows = ctx.store.all_embeddings()
    with ctx.glock:
        ctx.gallery.load([(r["member_id"], r["vec"], {"source": r["source"], "pitch": r["pitch"], "yaw": r["yaw"],
                                                       "img": r["img"], "step": r["step"]}) for r in rows])


def apply_settings() -> None:
    if hasattr(ctx, "presence"):
        ctx.presence.grace_s = int(S("grace_s"))
        ctx.presence.min_absence_s = int(S("min_absence_s"))


def thresholds(context: str, face: Face):
    high = False
    if face.pitch is not None and face.yaw is not None:
        high = posebins.is_high_angle(face.pitch, face.yaw, S("high_angle_pitch"), S("high_angle_yaw"))
    if context == "door":
        return S("door_threshold"), S("door_margin"), high
    return (S("room_threshold_high") if high else S("room_threshold")), S("room_margin"), high


def recognize_face(face: Face, context: str) -> dict:
    thr, margin, high = thresholds(context, face)
    with ctx.glock:
        d = ctx.gallery.decide(face.emb, thr, margin) if face.emb is not None else \
            {"member_id": None, "best_id": None, "score": -1.0, "margin": 0.0, "reason": "no_embedding"}
    d.update(threshold=thr, margin_required=margin, high_angle=high,
             name=ctx.store.member_name(d["member_id"]), best_name=ctx.store.member_name(d["best_id"]))
    return d


def _summarize(member_id: str, items: list) -> dict:
    """items: [(meta, index)] của một thành viên. Độ phủ/tiến độ chỉ tính mẫu "thật" (không tính bản che/augmentation)."""
    imgs = {md.get("img") for md, _ in items}
    by_source: Dict[str, int] = defaultdict(int)
    real, flips, aug, occ = [], 0, 0, 0
    for md, _ in items:
        by_source[md.get("source") or "?"] += 1
        st = md.get("step") or ""
        if "+" not in st:
            real.append(md)
        elif "+flip" in st:
            flips += 1
        elif "+aug-" in st:
            aug += 1
        elif "+occ-" in st:
            occ += 1
    cov_all = posebins.coverage([(md.get("pitch"), md.get("yaw")) for md, _ in items if "+aug-" not in (md.get("step") or "")
                                 and "+occ-" not in (md.get("step") or "")])
    pts = [(md["pitch"], md["yaw"]) for md in real if md.get("pitch") is not None and md.get("yaw") is not None]
    club = posebins.club_counts(pts)
    target = int(S("enroll_target"))
    min_dir = int(S("enroll_min_per_dir"))
    stored = getattr(ctx, "_image_counts", {}).get(member_id, 0)
    out = {"member_id": member_id, "name": ctx.store.member_name(member_id), "images": len(imgs),
           "images_stored": stored,
           "embeddings": len(items), "by_source": dict(by_source), "grid": cov_all["grid"], "profiles": {},
           "poses": [[round(p, 1), round(y, 1)] for p, y in pts],
           "real": len(real), "flips": flips, "aug": aug, "occ": occ, "club": club, "target": target,
           "progress": round(min(1.0, len(real) / target), 3) if target else 1.0,
           "club_missing": posebins.club_missing(club, min_dir),
           "complete": len(real) >= int(S("enroll_min_real")) and not posebins.club_missing(club, min_dir)}
    for prof in posebins.PROFILES:
        miss = posebins.missing(cov_all, prof)
        out["profiles"][prof] = {"score": round(posebins.score(cov_all, prof), 2),
                                 "missing": [{"pitch": p, "yaw": y, "label": posebins.cell_label(p, y)} for p, y in miss]}
    return out


def member_summary(member_id: str) -> dict:
    ctx._image_counts = ctx.store.image_counts()
    with ctx.glock:
        items = [(md, i) for i, (m, md) in enumerate(zip(ctx.gallery.ids, ctx.gallery.meta)) if m == member_id]
    return _summarize(member_id, items)


def all_summaries() -> List[dict]:
    """Tóm tắt mọi thành viên chỉ với MỘT lượt duyệt thư viện (không phải mỗi người một lượt)."""
    groups: Dict[str, list] = defaultdict(list)
    with ctx.glock:
        for i, (m, md) in enumerate(zip(ctx.gallery.ids, ctx.gallery.meta)):
            groups[m].append((md, i))
    ctx._image_counts = ctx.store.image_counts()
    return [_summarize(mid, groups.get(mid, [])) for mid, _ in ctx.store.members()]


def enroll_face(member_id: str, face: Optional[Face], source: str, step: str, flip: bool,
                dedup: float, img_id: str, jpeg: bytes = b"") -> dict:
    """Lưu một mặt (và bản lật) nếu đạt chất lượng. Trả về {accepted, reason, ...}."""
    if face is None:
        return {"accepted": False, "reason": "no_face"}
    gate = ENROLL_GATES[source]
    info = face.brief()
    if face.emb is None:
        return {"accepted": False, "reason": "no_embedding", **info}
    if face.det_score < gate["min_det"] or face.width < gate["min_w"]:
        return {"accepted": False, "reason": "small_or_unsure", **info}
    if face.blur < gate["min_blur"]:
        return {"accepted": False, "reason": "blurry", **info}
    if face.yaw is not None and (abs(face.yaw) > gate["max_yaw"] or abs(face.pitch) > gate["max_pitch"]):
        return {"accepted": False, "reason": "extreme_pose", **info}
    with ctx.glock:
        if len({md.get("img") for m, md in zip(ctx.gallery.ids, ctx.gallery.meta) if m == member_id}) >= MAX_IMAGES_PER_MEMBER:
            return {"accepted": False, "reason": "limit", **info}
        if ctx.gallery.max_sim(member_id, face.emb) > dedup:
            return {"accepted": False, "reason": "duplicate", **info}
    ts = now().isoformat()
    variants = [(face.emb, source, face.pitch, face.yaw, step)]
    meta = {"source": source, "pitch": face.pitch, "yaw": face.yaw, "img": img_id, "step": step}
    extra_meta = []
    for kind, ev in (face.occ or {}).items():
        variants.append((ev, source, face.pitch, face.yaw, step + "+occ-" + kind))
        extra_meta.append({**meta, "step": step + "+occ-" + kind, "emb": ev})
    if flip and face.emb_flip is not None:
        fy = None if face.yaw is None else -face.yaw
        variants.append((face.emb_flip, source, face.pitch, fy, step + "+flip"))
        extra_meta.append({**meta, "yaw": fy, "step": step + "+flip", "emb": face.emb_flip})
    ctx.store.add_sample_with_image(member_id, img_id, jpeg, "image/jpeg", ts, variants)
    with ctx.glock:
        ctx.gallery.add(member_id, face.emb, meta)
        for md in extra_meta:
            ctx.gallery.add(member_id, md.pop("emb"), md)
    return {"accepted": True, "reason": "ok", **info}


def day_bounds(day: Optional[date]):
    day = day or now().date()
    lo = datetime.combine(day, datetime.min.time())
    return day, lo.isoformat(), (lo + timedelta(days=1)).isoformat()


def shift_state(member_id: str, start: datetime, end: datetime, ts: datetime) -> str:
    if ts < start:
        return "upcoming"
    if ts >= end:
        return "done"
    return "active"


# --------------------------------------------------------------------- vòng đời
@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx.started = now()
    require_env()
    log.info("Kết nối database %s (APP_ENV=%s)", dsn_brief(DATABASE_URL), APP_ENV)
    ctx.store = Storage(DATABASE_URL)
    ctx.sessions = enrolling.SessionStore(backend=ctx.store)
    ctx.settings = config.defaults()
    for k, v in (ctx.store.kv_get("settings", {}) or {}).items():
        try:
            ctx.settings[k] = config.coerce(k, v)
        except ValueError:
            pass
    ctx.engine = None
    ctx.engine_status = "loading"
    ctx.engine_error = ""
    ctx.gallery = Gallery()
    load_gallery()

    def on_absence(a: Absence) -> None:
        ctx.store.add_absence(a.member_id, a.start.isoformat(), a.end.isoformat(), a.minutes)

    def on_event(ts: datetime, kind: str, member_id: str) -> None:
        ctx.store.add_event(ts.isoformat(), kind, member_id)

    ctx.presence = PresenceEngine(grace_s=int(S("grace_s")), min_absence_s=int(S("min_absence_s")),
                                  on_absence=on_absence, on_event=on_event)
    for _id, mid, s, e in ctx.store.all_shifts():
        ctx.presence.add_shift(Shift(mid, datetime.fromisoformat(s), datetime.fromisoformat(e)))
    restored = ctx.presence.restore(ctx.store.kv_get("presence", {}), now())
    raw_h = ctx.store.kv_get("handoff", {}) or {}
    for m, t0 in raw_h.items():
        try:
            ctx.handoff[m] = datetime.fromisoformat(t0)
        except (TypeError, ValueError):
            pass
    log.info("DB sẵn sàng: %d mẫu khuôn mặt, %d trạng thái hiện diện khôi phục; model=%s (tải nền)",
             len(ctx.gallery.ids), restored, default_model_name())

    def _load_engine_sync() -> FaceEngine:
        name = default_model_name()
        log.info("Đang tải InsightFace %s …", name)
        eng = FaceEngine(name)
        if not skip_model_warmup():
            warm = min(int(S("det_size_enroll")), 320)
            log.info("Làm nóng model (%d px) …", warm)
            eng.warmup(warm)
        else:
            log.info("Bỏ qua warmup model (SKIP_MODEL_WARMUP / Render)")
        return eng

    async def engine_loader():
        last_err = None
        for attempt in range(1, 4):
            try:
                ctx.engine = await asyncio.to_thread(_load_engine_sync)
                ctx.engine_status = "ready"
                log.info("Model sẵn sàng: %s providers=%s", ctx.engine.model_name, ctx.engine.providers)
                return
            except Exception as e:
                last_err = e
                ctx.engine_error = type(e).__name__
                log.warning("Tải model lần %d thất bại: %s", attempt, type(e).__name__)
                await asyncio.sleep(2 * attempt)
        ctx.engine_status = "failed"
        log.error("Không tải được model sau 3 lần: %s", ctx.engine_error)

    def tick_once():
        ts = now()
        with ctx.plock:
            ctx.presence.tick(ts)
            ctx.store.kv_set("presence", ctx.presence.snapshot())
            ctx.store.kv_set("handoff", {m: t.isoformat() for m, t in ctx.handoff.items()
                                         if (ts - t).total_seconds() <= HANDOFF_TTL_S})
        for m, t0 in list(ctx.handoff.items()):
            if (ts - t0).total_seconds() > HANDOFF_TTL_S:
                ctx.handoff.pop(m, None)

    async def ticker():
        while True:
            await asyncio.sleep(15)
            try:
                await asyncio.to_thread(tick_once)
            except Exception:
                log.exception("ticker lỗi")

    engine_task = asyncio.create_task(engine_loader())
    task = asyncio.create_task(ticker())
    yield
    engine_task.cancel()
    task.cancel()
    with ctx.plock:
        ctx.store.kv_set("presence", ctx.presence.snapshot())
        ctx.store.kv_set("handoff", {m: t.isoformat() for m, t in ctx.handoff.items()})


app = FastAPI(title="Club attendance", lifespan=lifespan)

# UI đặt ở nơi khác (vd Vercel) gọi API qua trình duyệt cần CORS. Đặt CORS_ORIGINS="https://ten-ban.vercel.app" (nhiều giá trị: phẩy).
# Xác thực bằng header X-API-Token (không dùng cookie) nên cho phép "*" cũng không lộ phiên đăng nhập; vẫn nên liệt kê cụ thể.
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"], max_age=600)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")


@app.get("/health")
@app.get("/api/health")
def health():
    db_ok = False
    try:
        if getattr(ctx, "store", None) is not None:
            ctx.store.ping()
            db_ok = True
    except Exception:
        db_ok = False
    engine = getattr(ctx, "engine", None)
    gallery = getattr(ctx, "gallery", None)
    body = {
        "ok": db_ok,
        "db": db_ok,
        "model_ready": engine is not None,
        "model_status": getattr(ctx, "engine_status", "unknown"),
        "providers": getattr(engine, "providers", []),
        "model": getattr(engine, "model_name", None) or default_model_name(),
        "embeddings": len(gallery.ids) if gallery is not None else 0,
        "members": len(gallery.members()) if gallery is not None else 0,
    }
    if not db_ok:
        return JSONResponse(body, status_code=503)
    return body


# --------------------------------------------------------------------- thành viên
class MemberIn(BaseModel):
    member_id: str
    name: str


@app.post("/api/members", dependencies=[Depends(auth)])
def api_upsert_member(m: MemberIn):
    mid = check_member_id(m.member_id)
    if not m.name.strip():
        raise HTTPException(400, "Tên không được để trống.")
    ctx.store.upsert_member(mid, m.name.strip(), now().isoformat())
    return {"ok": True, "member_id": mid}


@app.post("/members", dependencies=[Depends(auth)], include_in_schema=False)
def legacy_upsert_member(member_id: str = Form(...), name: str = Form(...)):
    return api_upsert_member(MemberIn(member_id=member_id, name=name))


@app.get("/api/members", dependencies=[Depends(auth)])
def api_members():
    return {"members": all_summaries()}


@app.get("/api/members/{member_id}", dependencies=[Depends(auth)])
def api_member(member_id: str):
    if not ctx.store.member_exists(member_id):
        raise HTTPException(404, "Không có thành viên này.")
    return member_summary(member_id)


@app.delete("/api/members/{member_id}", dependencies=[Depends(auth)])
def api_delete_member(member_id: str, keep_shifts: bool = False):
    ctx.store.delete_member(member_id, keep_shifts=keep_shifts)
    with ctx.glock:
        ctx.gallery.remove_member(member_id)
    with ctx.plock:
        ctx.presence.forget(member_id, keep_shifts=keep_shifts)
    ctx.handoff.pop(member_id, None)
    return {"ok": True}


@app.delete("/api/members/{member_id}/embeddings", dependencies=[Depends(auth)])
def api_delete_embeddings(member_id: str, source: Optional[str] = None):
    n = ctx.store.delete_embeddings(member_id, source)
    with ctx.glock:
        ctx.gallery.remove_member(member_id, source=source)
    return {"ok": True, "deleted": n}


# --------------------------------------------------------------------- đăng ký khuôn mặt
@app.post("/api/pose", dependencies=[Depends(auth)])
def api_pose(file: UploadFile = File(...), size: int = Form(320)):
    """Phát hiện nhanh + góc đầu (không tính embedding). Dùng cho HUD hướng dẫn xoay đầu."""
    img = read_image(file)
    if img is None:
        raise HTTPException(400, "Không đọc được ảnh.")
    h, w = img.shape[:2]
    faces = get_engine().analyze(img, det_size=max(160, min(960, size)), embed=False)
    return {"w": w, "h": h, "faces": [f.brief() for f in faces]}


@app.post("/api/enroll/frame", dependencies=[Depends(auth)])
def api_enroll_frame(file: UploadFile = File(...), member_id: str = Form(...), source: str = Form("door"),
                     step: str = Form(""), flip: bool = Form(True), dedup: float = Form(0.985),
                     occ: bool = Form(False)):
    """Lưu một khung hình từ HUD (mặt lớn nhất). Trả về lý do nếu bị từ chối."""
    member_id = check_member_id(member_id)
    if source not in ENROLL_GATES:
        raise HTTPException(400, "source phải là door hoặc room.")
    if not ctx.store.member_exists(member_id):
        raise HTTPException(404, "Chưa có thành viên này, hãy tạo trước.")
    img = read_image(file)
    if img is None:
        raise HTTPException(400, "Không đọc được ảnh.")
    face = get_engine().best_face(img, det_size=S("det_size_door") if source == "door" else S("det_size_room"), flip=flip, occ=occ)
    res = enroll_face(member_id, face, source, step, flip, dedup, uuid.uuid4().hex[:12], jpeg_of(img))
    res["summary"] = member_summary(member_id)
    return res


@app.post("/api/enroll/batch", dependencies=[Depends(auth)])
@app.post("/enroll", dependencies=[Depends(auth)], include_in_schema=False)
def api_enroll_batch(member_id: str = Form(...), source: str = Form("door"), max_keep: int = Form(20),
                     flip: bool = Form(True), dedup: float = Form(0.93), occ: bool = Form(False),
                     files: List[UploadFile] = File(...)):
    """Đăng ký từ nhiều ảnh/khung hình video (ví dụ video quay bằng cam trần). Giữ tối đa max_keep ảnh tốt."""
    member_id = check_member_id(member_id)
    if source not in ENROLL_GATES:
        raise HTTPException(400, "source phải là door hoặc room.")
    if not ctx.store.member_exists(member_id):
        ctx.store.upsert_member(member_id, member_id, now().isoformat())
    kept, skipped = 0, defaultdict(int)
    for up in files:
        if kept >= max_keep:
            break
        img = read_image(up)
        if img is None:
            skipped["bad_image"] += 1
            continue
        face = get_engine().best_face(img, det_size=S("det_size_door") if source == "door" else S("det_size_room"), flip=flip, occ=occ)
        r = enroll_face(member_id, face, source, "batch", flip, dedup, uuid.uuid4().hex[:12], jpeg_of(img))
        if r["accepted"]:
            kept += 1
        else:
            skipped[r["reason"]] += 1
    return {"member_id": member_id, "kept": kept, "skipped": dict(skipped),
            "total_for_member": ctx.gallery.count(member_id), "summary": member_summary(member_id)}


# ---- đăng ký kiểu CLB theo phiên (xem server/enrolling.py) -------------------------------------
REJECT_COUNTED = {"multi": "multiple_faces", "clipped": "clipped", "unsure": "low_det", "far": "small", "blurry": "blurry",
                  "no_face": "no_face", "bad_image": "bad_image"}


class SessionIn(BaseModel):
    member_id: str
    name: str = ""
    mode: str = "add"                # add (bổ sung vào mẫu đã có, mặc định) | replace (xoá mẫu cũ khi lưu)
    kind: str = "live"               # live (camera) | dataset (ảnh/video có sẵn)
    source: str = "door"


class FinishIn(BaseModel):
    target: Optional[int] = None
    flip: bool = True
    augment: str = "auto"            # auto (chỉ dataset và chỉ khi thiếu) | off | force
    verify: bool = True
    commit: bool = False


class UndoIn(BaseModel):
    member_id: str
    img_ids: List[str]


def get_session(sid: str) -> "enrolling.Session":
    sess = ctx.sessions.get(sid)
    if sess is None:
        raise HTTPException(404, "Phiên đăng ký đã hết hạn hoặc không tồn tại: bấm Bắt đầu để tạo phiên mới.")
    return sess


@app.post("/api/enroll/session", dependencies=[Depends(auth)])
def api_session_create(b: SessionIn):
    mid = check_member_id(b.member_id)
    if b.mode not in ("add", "replace") or b.kind not in ("live", "dataset") or b.source not in ENROLL_GATES:
        raise HTTPException(400, "mode phải là add/replace, kind là live/dataset, source là door/room.")
    exists = ctx.store.member_exists(mid)
    if b.kind == "live" and not exists:
        raise HTTPException(404, "Chưa có thành viên này, hãy tạo trước.")
    name = b.name.strip() or (ctx.store.member_name(mid) if exists else mid)
    sess = ctx.sessions.create(mid, name, b.mode, b.kind, b.source)
    return {"sid": sess.sid, "member_id": mid, "exists": exists, "ttl_s": enrolling.SESSION_TTL_S,
            "det_size": S("det_size_enroll"), "target": S("enroll_target")}


def _session_warning(sess) -> Optional[str]:
    """Cảnh báo sớm khi các khung của phiên giống một thành viên khác hơn chính người đang đăng ký."""
    cen = sess.centroid()
    if cen is None or sess.total() < 6:
        return None
    with ctx.glock:
        sc = ctx.gallery.scores(cen)
    own = sc.get(sess.member_id, -1.0)
    other_id, other = max(((k, v) for k, v in sc.items() if k != sess.member_id), key=lambda kv: kv[1], default=(None, -1.0))
    if other_id and other >= enrolling.LOOKS_LIKE_OTHER and other > own + 0.1:
        return f"Khuôn mặt này giống {ctx.store.member_name(other_id)} ({other:.2f}) hơn {sess.name}. Có chọn nhầm người không?"
    return None


def feed_session(sess, img: Optional[np.ndarray], label: str = "") -> dict:
    cfg = config.enroll_cfg(ctx.settings)
    if img is None:
        sess.rejects["bad_image"] += 1
        return {"state": "bad_image", "name": label, "kept": False}
    dataset = sess.kind == "dataset"
    info, cand = enrolling.observe(get_engine(), img, cfg, int(S("det_size_door") if dataset else S("det_size_enroll")), keep_other=dataset)
    kept = False
    if cand is not None and not dataset and sess.total() >= 6:      # đổi người giữa chừng
        cen = sess.centroid()
        if cen is not None and float(cand["emb"] @ cen) < cfg["outlier_cos"]:
            info["state"], cand = "other_person", None
    if cand is not None:
        kept = sess.add(cand, cfg)
        info["fid"] = cand["fid"]
        if not kept:
            info["state"] = "dup"
    sess.frames += 1
    if info["state"] in REJECT_COUNTED:
        sess.rejects[REJECT_COUNTED[info["state"]]] += 1
    info["kept"] = kept
    if label:
        info["name"] = label
    return info


@app.post("/api/enroll/session/{sid}/frames", dependencies=[Depends(auth)])
def api_session_frames(sid: str, files: List[UploadFile] = File(...)):
    """Gửi một hoặc vài khung hình. Mỗi khung chỉ được xử lý MỘT lần: phát hiện + góc + embedding (nếu đạt cổng)."""
    import time
    sess = get_session(sid)
    t0 = time.perf_counter()
    with sess.lock:
        results = [feed_session(sess, read_image(up), up.filename or "") for up in files]
        warn = _session_warning(sess) if sess.kind == "live" else None
    ctx.sessions.persist(sess)
    return {"results": results, "counts": sess.counts(), "total": sess.total(), "frames": sess.frames, "warning": warn,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


@app.post("/api/enroll/session/{sid}/reset", dependencies=[Depends(auth)])
def api_session_reset(sid: str, direction: Optional[str] = None):
    sess = get_session(sid)
    if direction is not None and direction not in posebins.CLUB_DIRS:
        raise HTTPException(400, "Hướng không hợp lệ.")
    sess.reset(direction)
    ctx.sessions.persist(sess)
    return {"counts": sess.counts(), "total": sess.total()}


def compute_plan(sess, body: FinishIn) -> dict:
    cfg = config.enroll_cfg(ctx.settings)
    with ctx.glock:
        own, other_rows = [], []
        for i, (m, md) in enumerate(zip(ctx.gallery.ids, ctx.gallery.meta)):
            if m == sess.member_id:
                own.append({"emb": ctx.gallery.mat[i].copy(), "pitch": md.get("pitch"), "yaw": md.get("yaw"),
                            "step": md.get("step") or "", "img": md.get("img")})
            else:
                other_rows.append(i)
        other_mat = ctx.gallery.mat[other_rows].copy() if other_rows else np.zeros((0, 512), np.float32)
        other_ids = [ctx.gallery.ids[i] for i in other_rows]
    names = {m: ctx.store.member_name(m) for m in set(other_ids)}
    opts = {"target": body.target, "flip": body.flip, "augment": body.augment, "verify": body.verify}
    return enrolling.build_plan(sess, get_engine(), cfg, own, other_mat, other_ids, names, opts,
                                MAX_IMAGES_PER_MEMBER, S("door_threshold"), S("door_margin"))


def commit_plan(sess) -> dict:
    plan = sess.pending
    if not plan or not plan["rows"]:
        raise HTTPException(409, plan["report"]["advice"] if plan else "Chưa chốt phiên.")
    mid = sess.member_id
    ts = now().isoformat()
    created = not ctx.store.member_exists(mid)
    upsert = created or bool(sess.name and sess.kind == "dataset")
    rows = plan["rows"]
    images: Dict[str, tuple] = {}
    for c in sess.all():
        iid = c.get("img_id")
        if iid and c.get("jpg"):
            images[iid] = (c["jpg"], "image/jpeg")
    bulk = [(mid, r["emb"], sess.source, r["pitch"], r["yaw"], ts, r["img"], r["step"]) for r in rows]
    try:
        ctx.store.commit_enrollment(mid, sess.name or mid, ts, upsert, bool(plan["replace"]), bulk, images)
    except Exception:
        log.exception("commit enrollment failed")
        raise HTTPException(500, "Không lưu được dữ liệu đăng ký.")
    with ctx.glock:
        if plan["replace"]:
            ctx.gallery.remove_member(mid)
        ctx.gallery.add_many([(mid, r["emb"], {"source": sess.source, "pitch": r["pitch"], "yaw": r["yaw"],
                                               "img": r["img"], "step": r["step"]}) for r in rows])
    ctx.sessions.delete(sess.sid)
    rep = plan["report"]
    return {"committed": True, "created": created, "report": rep, "img_ids": sorted({r["img"] for r in rows}),
            "total_for_member": ctx.gallery.count(mid), "summary": member_summary(mid)}


@app.post("/api/enroll/session/{sid}/finish", dependencies=[Depends(auth)])
def api_session_finish(sid: str, body: FinishIn = Body(default=FinishIn())):
    """Chọn 20–30 mẫu tốt nhất từ các khung đã gửi, (dataset) bù augmentation nếu thiếu, kiểm tra độ tin cậy.
    commit=false: chỉ trả báo cáo (chưa ghi), gọi /commit để lưu. commit=true: ghi luôn."""
    if body.augment not in ("auto", "off", "force"):
        raise HTTPException(400, "augment phải là auto, off hoặc force.")
    sess = get_session(sid)
    with sess.lock:
        sess.pending = compute_plan(sess, body)
        ctx.sessions.persist(sess)
        if body.commit and sess.pending["rows"]:
            return commit_plan(sess)
        rep = sess.pending["report"]
    return {"committed": False, "report": rep}


@app.post("/api/enroll/session/{sid}/commit", dependencies=[Depends(auth)])
def api_session_commit(sid: str):
    sess = get_session(sid)
    with sess.lock:
        return commit_plan(sess)


@app.delete("/api/enroll/session/{sid}", dependencies=[Depends(auth)])
def api_session_delete(sid: str):
    return {"ok": True, "deleted": ctx.sessions.delete(sid)}


@app.post("/api/enroll/undo", dependencies=[Depends(auth)])
def api_enroll_undo(b: UndoIn):
    """Hoàn tác một lần lưu (bằng danh sách img_ids trả về lúc commit). Không khôi phục mẫu cũ nếu đã chọn 'replace'."""
    mid = check_member_id(b.member_id)
    n = ctx.store.delete_embeddings_by_img(mid, b.img_ids)
    with ctx.glock:
        ctx.gallery.remove_imgs(mid, b.img_ids)
    return {"ok": True, "deleted": n, "summary": member_summary(mid) if ctx.store.member_exists(mid) else None}


DATASET_REASONS = ("bad_image", "no_face", "multiple_faces", "clipped", "low_det", "small", "blurry", "looks_like_other", "outlier")


@app.post("/api/enroll/dataset", dependencies=[Depends(auth)])
def api_enroll_dataset(member_id: str = Form(...), name: str = Form(""), source: str = Form("door"),
                       target: int = Form(0), mode: str = Form("add"), flip: bool = Form(True), augment: str = Form("auto"),
                       verify: bool = Form(True), files: List[UploadFile] = File(...),
                       per_cell: int = Form(0), max_keep: int = Form(0), occ: bool = Form(False), dedup: float = Form(0)):
    """Nhập dataset CLB đã thu sẵn trong MỘT yêu cầu (tương thích bản cũ). Dùng đúng bộ chọn mẫu của đăng ký trực tiếp:
    chấm điểm -> loại 2 mặt/ngoại lai/giống người khác -> giữ 20–30 mẫu cân bằng 5 hướng -> augmentation nếu thiếu.
    per_cell/max_keep/occ/dedup chỉ giữ để tương thích, không còn tác dụng. Dataset lớn nên dùng /api/enroll/session."""
    mid = check_member_id(member_id)
    if source not in ENROLL_GATES or mode not in ("add", "replace") or augment not in ("auto", "off", "force"):
        raise HTTPException(400, "Tham số source/mode/augment không hợp lệ.")
    sess = ctx.sessions.create(mid, name.strip() or (ctx.store.member_name(mid) if ctx.store.member_exists(mid) else mid),
                               mode, "dataset", source)
    try:
        with sess.lock:
            for up in files:
                feed_session(sess, read_image(up), up.filename or "")
            sess.pending = compute_plan(sess, FinishIn(target=target or None, flip=flip, augment=augment, verify=verify))
            rep = sess.pending["report"]
            if not sess.pending["rows"]:
                return {"member_id": mid, "kept": 0, "skipped": rep["rejected"], "warnings": rep["warnings"], "created": False,
                        "total_for_member": ctx.gallery.count(mid), "summary": member_summary(mid) if ctx.store.member_exists(mid) else None,
                        "report": rep}
            out = commit_plan(sess)
    finally:
        ctx.sessions.delete(sess.sid)
    return {"member_id": mid, "kept": rep["kept"], "skipped": rep["rejected"], "warnings": rep["warnings"], "created": out["created"],
            "total_for_member": out["total_for_member"], "summary": out["summary"], "report": rep, "img_ids": out["img_ids"]}


# --------------------------------------------------------------------- thử nhận diện / hiệu chỉnh
@app.post("/api/recognize", dependencies=[Depends(auth)])
def api_recognize(file: UploadFile = File(...), context: str = Form("room"), stranger: bool = Form(False)):
    """Nhận diện mọi mặt trong ảnh, trả lý do chấp nhận/từ chối. stranger=1: lưu các mặt này làm mẫu người lạ."""
    if context not in ("door", "room"):
        raise HTTPException(400, "context phải là door hoặc room.")
    img = read_image(file)
    if img is None:
        raise HTTPException(400, "Không đọc được ảnh.")
    faces = get_engine().analyze(img, det_size=S("det_size_door") if context == "door" else S("det_size_room"))
    out, saved = [], 0
    for f in faces:
        d = recognize_face(f, context)
        d.update(f.brief())
        out.append(d)
        if stranger and f.emb is not None:
            ctx.store.add_stranger(f.emb, f.pitch, f.yaw, context, now().isoformat())
            saved += 1
    return {"w": img.shape[1], "h": img.shape[0], "faces": out, "stranger_saved": saved}


@app.get("/api/calibration", dependencies=[Depends(auth)])
def api_calibration():
    rows = ctx.store.all_embeddings()
    rep = calibration.analyze(rows, ctx.store.strangers(), S("high_angle_pitch"), S("high_angle_yaw"))
    rep["current"] = {k: S(k) for k in ("door_threshold", "room_threshold", "room_threshold_high",
                                        "door_margin", "room_margin")}
    return rep


@app.delete("/api/strangers", dependencies=[Depends(auth)])
def api_clear_strangers():
    return {"deleted": ctx.store.clear_strangers()}


@app.get("/api/settings", dependencies=[Depends(auth)])
def api_get_settings():
    return {"values": ctx.settings, "spec": config.describe()}


@app.post("/api/settings", dependencies=[Depends(auth)])
def api_set_settings(values: Dict[str, Any] = Body(...)):
    new = dict(ctx.settings)
    for k, v in values.items():
        try:
            new[k] = config.coerce(k, v)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if new["close_hour"] <= new["open_hour"]:
        raise HTTPException(400, "Giờ đóng cửa phải sau giờ mở cửa.")
    ctx.settings = new
    ctx.store.kv_set("settings", new)
    apply_settings()
    return {"values": ctx.settings}


# --------------------------------------------------------------------- check-in (cam cửa)
@app.post("/api/checkin", dependencies=[Depends(auth)])
@app.post("/checkin", dependencies=[Depends(auth)], include_in_schema=False)
def checkin(files: List[UploadFile] = File(...)):
    """Nhận một burst vài khung hình; bỏ phiếu để quyết định danh tính."""
    votes: Dict[str, list] = defaultdict(list)
    usable, frames = 0, []
    for up in files:
        img = read_image(up)
        if img is None:
            continue
        f = get_engine().best_face(img, det_size=S("det_size_door"))
        if f is None:
            frames.append({"reason": "no_face"})
            continue
        if f.width < DOOR_MIN_W:
            frames.append({"reason": "too_small", "width": f.width})
            continue
        if f.yaw is not None and (abs(f.yaw) > DOOR_MAX_YAW or abs(f.pitch) > DOOR_MAX_PITCH):
            frames.append({"reason": "extreme_pose", "pitch": round(f.pitch), "yaw": round(f.yaw)})
            continue
        usable += 1
        d = recognize_face(f, "door")
        frames.append({"reason": d["reason"], "score": d["score"], "margin": d["margin"], "best": d["best_id"]})
        if d["member_id"]:
            votes[d["member_id"]].append((d["score"], d["margin"]))
    if usable == 0:
        return {"status": "no_usable_face", "frames": frames}
    if not votes:
        return {"status": "unknown", "usable_frames": usable, "frames": frames}
    who, vs = max(votes.items(), key=lambda kv: len(kv[1]))
    if len(vs) < int(S("door_votes")):
        return {"status": "unknown", "usable_frames": usable, "best_votes": len(vs), "frames": frames}
    ts = now()
    with ctx.plock:
        result = ctx.presence.checkin(who, ts)
        on_shift = ctx.presence.on_shift(who, ts)
        ctx.store.kv_set("presence", ctx.presence.snapshot())
    if result == "checkin":
        ctx.handoff[who] = ts
        ctx.store.kv_set("handoff", {m: t.isoformat() for m, t in ctx.handoff.items()})
    return {"status": result, "member_id": who, "name": ctx.store.member_name(who),
            "score": round(float(np.mean([v[0] for v in vs])), 3), "votes": len(vs),
            "time": ts.isoformat(timespec="seconds"), "on_shift": on_shift, "frames": frames}


# --------------------------------------------------------------------- cam trong phòng
@app.post("/api/room/frame", dependencies=[Depends(auth)])
@app.post("/observe", dependencies=[Depends(auth)], include_in_schema=False)
def room_frame(file: UploadFile = File(...)):
    """Chế độ không theo dõi người: nhận một khung hình, nhận diện mọi mặt thấy được."""
    img = read_image(file)
    if img is None:
        raise HTTPException(400, "Không đọc được ảnh.")
    ts, out = now(), []
    need = int(S("room_votes"))
    for f in get_engine().analyze(img, det_size=S("det_size_room")):
        d = recognize_face(f, "room")
        who = d["member_id"]
        confirmed = False
        if who:
            vs = [t for t in ctx.frame_votes.get(who, []) if (ts - t).total_seconds() <= 15] + [ts]
            ctx.frame_votes[who] = vs
            if len(vs) >= need:
                confirmed = True
                with ctx.plock:
                    ctx.presence.observe(who, ts)
        d.update(f.brief())
        d["confirmed"] = confirmed
        out.append(d)
    with ctx.plock:
        occ = ctx.presence.occupants()
    ctx.room.update(persons=len(out), ts=ts.isoformat(timespec="seconds"))
    return {"w": img.shape[1], "h": img.shape[0], "faces": out, "occupants": occ}


@app.post("/api/room/identify", dependencies=[Depends(auth)])
def room_identify(track_ids: str = Form(...), files: List[UploadFile] = File(...)):
    """Nhận ảnh cắt (đầu-vai) của các track chưa biết danh tính; trả kết quả nhận diện cho từng track."""
    ids = [int(x) for x in track_ids.split(",") if x.strip() != ""]
    if len(ids) != len(files):
        raise HTTPException(400, "Số track_ids phải bằng số ảnh.")
    results = []
    for tid, up in zip(ids, files):
        img = read_image(up)
        res = {"track_id": tid, "face_found": False, "member_id": None, "score": 0.0}
        if img is not None:
            faces = get_engine().analyze_crop(img)
            if faces:
                f = faces[0]
                d = recognize_face(f, "room")
                res.update(face_found=True, member_id=d["member_id"], score=d["score"], margin=d["margin"],
                           reason=d["reason"], high=d["high_angle"], pitch=f.brief()["pitch"], yaw=f.brief()["yaw"])
        results.append(res)
    return {"results": results}


class IdentIn(BaseModel):
    member_id: str
    score: float = 0.0
    source: str = "face"


class RoomUpdateIn(BaseModel):
    persons: int = 0
    strangers: int = 0
    identified: List[IdentIn] = Field(default_factory=list)
    bound: List[str] = Field(default_factory=list)


@app.post("/api/room/update", dependencies=[Depends(auth)])
def room_update(u: RoomUpdateIn):
    """Cam trần báo định kỳ: số người, ai đang thấy. Trả về danh sách người vừa check-in chờ gán track."""
    ts = now()
    with ctx.plock:
        for it in u.identified:
            if ctx.store.member_exists(it.member_id):
                ctx.presence.observe(it.member_id, ts)
    for m in u.bound:
        ctx.handoff.pop(m, None)
    ctx.room.update(persons=u.persons, strangers=u.strangers, ts=ts.isoformat(timespec="seconds"),
                    identified=[i.member_id for i in u.identified])
    pending = [m for m, t0 in sorted(ctx.handoff.items(), key=lambda kv: kv[1])
               if (ts - t0).total_seconds() <= HANDOFF_TTL_S]
    return {"ok": True, "pending": pending}


# --------------------------------------------------------------------- trạng thái / ca trực / báo cáo
@app.get("/api/status", dependencies=[Depends(auth)])
def api_status():
    ts = now()
    names = dict(ctx.store.members())
    with ctx.plock:
        people = []
        for m, st in ctx.presence.state.items():
            if st.status in (PRESENT, AWAY):
                people.append({"member_id": m, "name": names.get(m, m), "status": st.status,
                               "last_seen": st.last_seen.isoformat(timespec="seconds") if st.last_seen else None,
                               "since": (st.away_from or st.checked_in_at).isoformat(timespec="seconds")
                               if (st.away_from or st.checked_in_at) else None,
                               "on_shift": ctx.presence.on_shift(m, ts)})
    day, lo, hi = day_bounds(ts.date())
    shifts = []
    for sid, m, s, e in ctx.store.shifts_between(lo, hi):
        st_, en_ = datetime.fromisoformat(s), datetime.fromisoformat(e)
        ab = ctx.store.absences_for(m, s, e)
        shifts.append({"id": sid, "member_id": m, "name": names.get(m, m), "start": s, "end": e,
                       "state": shift_state(m, st_, en_, ts), "status": ctx.presence.status_of(m),
                       "absences": [{"from": a, "to": b, "minutes": mins} for a, b, mins in ab]})
    events = [{"ts": t, "kind": k, "member_id": m, "name": names.get(m, m or "")} for t, k, m, _ in ctx.store.recent_events(30)]
    return {"now": ts.isoformat(timespec="seconds"), "people": people, "shifts": shifts, "events": events,
            "room": ctx.room, "pending_handoff": list(ctx.handoff),
            "open": S("open_hour") <= ts.hour < S("close_hour"),
            "counts": {"members": len(names), "embeddings": len(ctx.gallery.ids), "strangers": int(len(ctx.store.strangers()))},
            "engine": (
                {"providers": ctx.engine.providers, "model": ctx.engine.model_name}
                if ctx.engine is not None
                else {"providers": [], "model": default_model_name(), "status": ctx.engine_status}
            ),
            "settings": {k: S(k) for k in ("open_hour", "close_hour", "grace_s")}}


class ShiftIn(BaseModel):
    member_id: str
    start: datetime
    end: datetime


@app.get("/api/shifts", dependencies=[Depends(auth)])
def api_shifts(day: Optional[date] = None):
    day, lo, hi = day_bounds(day)
    names, ts = dict(ctx.store.members()), now()
    return {"day": str(day), "shifts": [
        {"id": i, "member_id": m, "name": names.get(m, m), "start": s, "end": e,
         "state": shift_state(m, datetime.fromisoformat(s), datetime.fromisoformat(e), ts)}
        for i, m, s, e in ctx.store.shifts_between(lo, hi)]}


@app.post("/api/shifts", dependencies=[Depends(auth)])
@app.post("/shifts", dependencies=[Depends(auth)], include_in_schema=False)
def add_shift(s: ShiftIn):
    st, en = to_local_naive(s.start), to_local_naive(s.end)
    if en <= st:
        raise HTTPException(400, "Giờ kết thúc phải sau giờ bắt đầu.")
    if st.date() != en.date():
        raise HTTPException(400, "Mỗi ca nằm trong một ngày; hãy tách ca qua đêm thành hai ca.")
    if not ctx.store.member_exists(s.member_id):
        raise HTTPException(404, "Chưa có thành viên này.")
    _d, lo, hi = day_bounds(st.date())
    for _sid, m, a, b in ctx.store.shifts_between(lo, hi):
        if m == s.member_id and a < en.isoformat() and b > st.isoformat():
            raise HTTPException(409, "Ca này trùng hoặc chồng lên một ca đã có của người này.")
    sid = ctx.store.add_shift(s.member_id, st.isoformat(), en.isoformat())
    with ctx.plock:
        ctx.presence.add_shift(Shift(s.member_id, st, en))
    return {"ok": True, "id": sid}


@app.delete("/api/shifts/{shift_id}", dependencies=[Depends(auth)])
def delete_shift(shift_id: int):
    r = ctx.store.get_shift(shift_id)
    if not r:
        raise HTTPException(404, "Không có ca này.")
    ctx.store.delete_shift(shift_id)
    with ctx.plock:
        ctx.presence.remove_shift(r[1], datetime.fromisoformat(r[2]), datetime.fromisoformat(r[3]))
    return {"ok": True}


def build_report(day: Optional[date]) -> dict:
    day, lo, hi = day_bounds(day)
    names = dict(ctx.store.members())
    ts = now()
    events_raw = ctx.store.events_between(lo, hi)
    first_in: Dict[str, str] = {}
    for t, k, m, _ in events_raw:
        if k in ("checkin", "seen_without_checkin") and m and m not in first_in:
            first_in[m] = t
    rows = []
    for sid, mid, s, e in ctx.store.shifts_between(lo, hi):
        st_, en_ = datetime.fromisoformat(s), datetime.fromisoformat(e)
        ab = [{"from": datetime.fromisoformat(a).strftime("%H:%M"), "to": datetime.fromisoformat(b).strftime("%H:%M"),
               "minutes": m, "ongoing": False} for a, b, m in ctx.store.absences_for(mid, s, e)]
        with ctx.plock:
            stt = ctx.presence.state.get(mid)
            away_from = stt.away_from if (stt and stt.status == AWAY) else None
        if away_from and st_ <= ts < en_:                      # đang vắng, chưa quay lại
            start = max(away_from, st_)
            ab.append({"from": start.strftime("%H:%M"), "to": "đang vắng", "minutes": round((ts - start).total_seconds() / 60, 1),
                       "ongoing": True})
        seen = first_in.get(mid)
        rows.append({"member_id": mid, "name": names.get(mid, mid),
                     "shift": [st_.strftime("%H:%M"), en_.strftime("%H:%M")],
                     "first_seen": seen[11:16] if seen else None,
                     "not_seen": (seen is None and ts >= st_),          # đã tới giờ ca mà chưa thấy check-in
                     "absences": ab, "total_minutes": round(sum(x["minutes"] for x in ab), 1)})
    events = [{"ts": t, "kind": k, "member_id": m, "name": names.get(m, m or "")} for t, k, m, _ in events_raw]
    with ctx.plock:
        occ = ctx.presence.occupants()
    return {"day": str(day), "shifts": rows, "events": events, "occupants_now": occ}


@app.get("/api/report", dependencies=[Depends(auth)])
@app.get("/report", dependencies=[Depends(auth)], include_in_schema=False)
def api_report(day: Optional[date] = None):
    return build_report(day)


def _csv_safe(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@app.get("/api/report.csv", dependencies=[Depends(auth)])
def api_report_csv(day: Optional[date] = None):
    rep = build_report(day)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Ngày", "Mã", "Tên", "Ca từ", "Ca đến", "Check-in đầu", "Vắng từ", "Vắng đến", "Số phút vắng", "Tổng phút vắng trong ca"])
    for r in rep["shifts"]:
        head = [rep["day"], _csv_safe(r["member_id"]), _csv_safe(r["name"]), *r["shift"], r["first_seen"] or "KHÔNG THẤY"]
        if not r["absences"]:
            w.writerow([*head, "", "", "", 0])
        for a in r["absences"]:
            w.writerow([*head, a["from"], a["to"], a["minutes"], r["total_minutes"]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="bao-cao-{rep["day"]}.csv"'})


@app.get("/api/members/{member_id}/images/{img_id}", dependencies=[Depends(auth)])
def api_member_image(member_id: str, img_id: str):
    """Tải một ảnh đã lưu. Cần token. Không liệt kê binary trong API list."""
    if not ctx.store.member_exists(member_id):
        raise HTTPException(404, "Không có thành viên này.")
    row = ctx.store.get_member_image(member_id, img_id)
    if not row:
        raise HTTPException(404, "Không có ảnh này.")
    data, mime, _n = row
    return Response(content=data, media_type=mime or "image/jpeg")


@app.get("/api/backup", dependencies=[Depends(auth)])
@app.get("/backup", dependencies=[Depends(auth)], include_in_schema=False)
def backup():
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    ctx.store.backup_to(tmp.name)
    return FileResponse(tmp.name, filename="club-backup.zip", background=BackgroundTask(os.unlink, tmp.name))


app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
