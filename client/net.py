"""Dùng chung cho các client: gọi server, giờ hoạt động."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Ho_Chi_Minh"))
except Exception:
    TZ = timezone(timedelta(hours=7))


class Server:
    def __init__(self, url, token, hf_token=None, timeout=90):
        self.url = url.rstrip("/")
        self.h = {"X-API-Token": token}
        if hf_token:                       # Space private (cũ): Hugging Face dùng header Authorization
            self.h["Authorization"] = "Bearer " + hf_token
        self.timeout = timeout
        self.s = requests.Session()

    def _raise(self, r):
        if r.status_code in (401, 403):
            raise requests.HTTPError("401/403: sai hoặc thiếu API_TOKEN (hoặc CORS).", response=r)
        r.raise_for_status()

    def post(self, path, **kw):
        r = self.s.post(self.url + path, headers=self.h, timeout=self.timeout, **kw)
        self._raise(r)
        return r.json()

    def delete(self, path, **kw):
        r = self.s.delete(self.url + path, headers=self.h, timeout=self.timeout, **kw)
        self._raise(r)
        return r.json()

    def get(self, path, **kw):
        last = None
        for attempt in range(2):
            try:
                r = self.s.get(self.url + path, headers=self.h, timeout=self.timeout, **kw)
                if r.status_code == 503 and attempt == 0:
                    time.sleep(3)
                    continue
                self._raise(r)
                return r.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                if attempt == 0:
                    time.sleep(3)
                    continue
                raise
        if last:
            raise last
        raise RuntimeError("get failed")


def from_env(args):
    token = args.token or os.getenv("API_TOKEN", "")
    if not token:
        sys.exit("Thiếu API token: dùng --token hoặc biến môi trường API_TOKEN.")
    return Server(args.url or os.getenv("SERVER_URL", "http://127.0.0.1:7860"), token, os.getenv("HF_TOKEN"))


def is_open(open_h, close_h):
    h = datetime.now(TZ).hour
    return open_h <= h < close_h


def add_common(ap):
    ap.add_argument("--url", help="địa chỉ server (hoặc SERVER_URL)")
    ap.add_argument("--token", help="API token (hoặc API_TOKEN)")
    ap.add_argument("--source", default="0", help="chỉ số webcam (0,1..) hoặc đường dẫn video / rtsp://")
    ap.add_argument("--open", type=int, default=8)
    ap.add_argument("--close", type=int, default=20)
    ap.add_argument("--always", action="store_true", help="bỏ qua giờ mở/đóng (để thử)")
    ap.add_argument("--show", action="store_true", help="hiện cửa sổ xem trước (cần opencv-python, không headless)")


def open_capture(src):
    import cv2
    cap = cv2.VideoCapture(int(src) if str(src).isdigit() else src)
    if not cap.isOpened():
        sys.exit(f"Không mở được camera/nguồn: {src}")
    return cap


def jpeg(img, q=88):
    import cv2
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])[1].tobytes()
