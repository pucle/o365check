"""Chuẩn hoá và kiểm tra ảnh đầu vào. Không tin MIME do client gửi. Không log nội dung ảnh."""
from __future__ import annotations

from typing import Optional, Tuple

import cv2
import numpy as np

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 12_000_000
JPEG_QUALITY = 88
ALLOWED_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),  # cần kiểm thêm WEBP
    (b"BM", "image/bmp"),
)


class ImageError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def sniff_mime(data: bytes) -> Optional[str]:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for magic, mime in ALLOWED_MAGIC:
        if magic != b"RIFF" and data.startswith(magic):
            return mime
    return None


def decode_checked(data: bytes, max_bytes: int = MAX_UPLOAD_BYTES) -> np.ndarray:
    if not data:
        raise ImageError("Ảnh rỗng.")
    if len(data) > max_bytes:
        raise ImageError(f"Ảnh quá lớn (tối đa {max_bytes // (1024 * 1024)} MB).", 413)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ImageError("Không giải mã được ảnh (định dạng không hỗ trợ hoặc file hỏng).")
    h, w = img.shape[:2]
    if h < 8 or w < 8:
        raise ImageError("Ảnh quá nhỏ.")
    if h * w > MAX_PIXELS:
        raise ImageError("Ảnh có quá nhiều pixel.", 413)
    return img


def to_jpeg(img: np.ndarray, quality: int = JPEG_QUALITY) -> Tuple[bytes, str]:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok or buf is None or len(buf) == 0:
        raise ImageError("Không chuẩn hoá được ảnh sang JPEG.")
    return buf.tobytes(), "image/jpeg"
