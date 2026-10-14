"""Đăng ký hàng loạt từ thư mục ảnh/video:  data_faces/<ma_thanh_vien>/*.jpg|*.mp4
    python tools/enroll.py data_faces --source room --url http://127.0.0.1:7860 --token XXX
Tên thành viên lấy từ tên thư mục (có thể đổi sau ở giao diện)."""
import argparse, glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cv2
from client import net

IMG = (".jpg", ".jpeg", ".png", ".bmp", ".webp"); VID = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def video_frames(path, n):
    cap = cv2.VideoCapture(path); total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0; out = []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + .5) / n)); ok, fr = cap.read()
        if ok: out.append(fr)
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("folder"); net.add_common(ap)
    ap.add_argument("--source", default="door", choices=["door", "room"]); ap.add_argument("--keep", type=int, default=25)
    ap.add_argument("--frames", type=int, default=50)
    a = ap.parse_args(); srv = net.from_env(a)
    for d in sorted(p for p in glob.glob(os.path.join(a.folder, "*")) if os.path.isdir(p)):
        mid = os.path.basename(d); files = []
        for f in sorted(glob.glob(os.path.join(d, "*"))):
            e = os.path.splitext(f)[1].lower()
            if e in IMG: files.append(open(f, "rb").read())
            elif e in VID: files += [net.jpeg(fr, 92) for fr in video_frames(f, a.frames)]
        if not files: continue
        srv.post("/api/members", json={"member_id": mid, "name": mid})
        kept, skipped = 0, {}
        for i in range(0, len(files), 6):
            if kept >= a.keep: break
            r = srv.post("/api/enroll/batch", data={"member_id": mid, "source": a.source, "max_keep": a.keep - kept, "dedup": 0.97},
                         files=[("files", (f"{j}.jpg", b)) for j, b in enumerate(files[i:i + 6])])
            kept += r["kept"]
            for k, v in r["skipped"].items(): skipped[k] = skipped.get(k, 0) + v
        print(f"{mid}: lưu {kept}/{len(files)} ảnh; bỏ qua {skipped}")


if __name__ == "__main__":
    main()
