"""Kiểm tra cấu hình Render và (tuỳ chọn) kích hoạt deploy dịch vụ đã có.

Không tạo dịch vụ tính phí, không đổi plan. Không in secret.

    python tools/deploy_render.py --check
    python tools/deploy_render.py --deploy --service-id srv-xxxx   # cần RENDER_API_KEY

Blueprint: render.yaml. Secrets phải đặt tay trên dashboard (API_TOKEN, DATABASE_URL).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
REQUIRED_SECRETS = ("API_TOKEN", "DATABASE_URL")
OPTIONAL = ("TZ_NAME", "CORS_ORIGINS", "MODEL_NAME", "APP_ENV", "INSIGHTFACE_HOME", "PORT")


def load_blueprint() -> dict:
    path = os.path.join(ROOT, "render.yaml")
    if not os.path.isfile(path):
        sys.exit("Thiếu render.yaml")
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        text = open(path, encoding="utf-8").read()
        if "type: web" not in text or "runtime: docker" not in text:
            sys.exit("render.yaml không có web/docker (cài PyYAML để phân tích đầy đủ, hoặc kiểm tra tay).")
        return {"_raw": True}


def check() -> int:
    ok = True
    bp = load_blueprint()
    print("render.yaml:", os.path.join(ROOT, "render.yaml"))
    docker = os.path.join(ROOT, "Dockerfile")
    print("Dockerfile:", "có" if os.path.isfile(docker) else "THIẾU")
    ok &= os.path.isfile(docker)
    if not bp.get("_raw"):
        services = bp.get("services") or []
        webs = [s for s in services if s.get("type") == "web"]
        if not webs:
            print("FAIL blueprint không có web service")
            ok = False
        for s in webs:
            plan = (s.get("plan") or "free").lower()
            print(f"  service={s.get('name')} runtime={s.get('runtime')} plan={plan}")
            if plan not in ("free", "", "starter"):
                print("  CẢNH BÁO: plan không phải free — không tự đổi; kiểm tra dashboard trước khi deploy.")
            if s.get("scaling") or s.get("instanceCount", 1) not in (1, None):
                print("  CẢNH BÁO: có cấu hình scale/instance — Render Free thường 1 instance.")
    print("Secrets bắt buộc trên Render (đặt tay, sync: false):", ", ".join(REQUIRED_SECRETS))
    print("Tuỳ chọn:", ", ".join(OPTIONAL))
    print("Local DATABASE_URL:", "đã đặt" if os.getenv("DATABASE_URL") else "chưa đặt")
    print("Local API_TOKEN:", "đã đặt" if os.getenv("API_TOKEN") else "chưa đặt")
    if os.getenv("DATABASE_URL"):
        from server.db import dsn_brief
        print("  DSN:", dsn_brief(os.getenv("DATABASE_URL", "")))
    print("Pipeline Hugging Face (tools/deploy_hf.py) tách biệt; Render là cách triển khai chính.")
    print("CHECK OK" if ok else "CHECK FAILED")
    return 0 if ok else 1


def deploy(service_id: str) -> int:
    key = os.getenv("RENDER_API_KEY", "").strip()
    if not key:
        print("Thiếu RENDER_API_KEY. Đặt secret trên dashboard rồi bấm Deploy thủ công, hoặc export RENDER_API_KEY.")
        return 2
    import urllib.request
    url = f"https://api.render.com/v1/services/{service_id}/deploys"
    req = urllib.request.Request(url, data=json.dumps({"clearCache": "do_not_clear"}).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        did = (body.get("id") or body.get("deploy", {}).get("id") or "?")
        print("Đã tạo deploy", did, "cho", service_id)
        print("Theo dõi log trên dashboard Render. Không tuyên bố thành công cho đến khi /api/health trả ok.")
        return 0
    except Exception as e:
        print("Không gọi được Render API:", type(e).__name__, str(e)[:200])
        return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", default=True)
    ap.add_argument("--deploy", action="store_true")
    ap.add_argument("--service-id", default=os.getenv("RENDER_SERVICE_ID", ""))
    a = ap.parse_args()
    code = check()
    if a.deploy:
        if not a.service_id:
            print("Cần --service-id srv-... (dịch vụ đã tạo trên Render). Script không tạo service mới.")
            return 2
        code = deploy(a.service_id) or code
    return code


if __name__ == "__main__":
    sys.exit(main())
