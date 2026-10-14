# Triển khai Club Attendance trên Render Free + Neon PostgreSQL

Đây là **cách triển khai chính**. Hugging Face Spaces (`tools/deploy_hf.py`) chỉ còn tương thích ngược, tách khỏi pipeline Render.

Render Free **ngủ** khi không có traffic, **khởi động lại** và có **giới hạn CPU/RAM**. Neon Free có **giới hạn dung lượng và compute**. Tài liệu này không cam kết uptime, hiệu năng, dung lượng hay miễn phí vô hạn.

**Bạn tự làm trên Neon / Render / GitHub** (tài khoản và secret không nên gửi vào chat hay commit). Repo đã có `Dockerfile`, `render.yaml`, migration và adapter PostgreSQL.

---

## Checklist nhanh (lần đầu)

| # | Việc | Xong? |
|---|------|-------|
| 1 | Tạo project Neon, lấy `DATABASE_URL` **pooled** (`sslmode=require`) | ☐ |
| 2 | (Tuỳ chọn) Chạy `python tools/migrate.py` trên máy để thử kết nối | ☐ |
| 3 | Đẩy code lên GitHub (không có `.env`, `*.db`) | ☐ |
| 4 | Render: Web Service **Docker**, plan **Free**, health `/api/health` | ☐ |
| 5 | Render Environment: `API_TOKEN`, `DATABASE_URL`, `APP_ENV=production` | ☐ |
| 6 | Đợi build xong (5–15 phút lần đầu), trạng thái **Live** | ☐ |
| 7 | Mở `https://<tên-dịch-vụ>.onrender.com/ui/` → nhập token → thử | ☐ |
| 8 | `python tools/smoke_test.py https://... --token ... --db-url ...` | ☐ |

---

## A. Tạo Neon database

1. Đăng nhập [Neon](https://console.neon.tech/) → **New Project** (ví dụ `club-attendance`).
2. Giữ database mặc định (thường `neondb`) hoặc tạo database mới.
3. Mở **Dashboard → Connect** (Connection details).
4. Chọn:
   - **Branch**: `main` / production (nhánh bạn dùng lâu dài).
   - **Connection pooling**: **On** (khuyến nghị cho Render).
   - **SSL**: bật; chuỗi kết nối phải có `sslmode=require` (Neon thường gắn sẵn).
5. Copy **connection string** dạng PostgreSQL. Ví dụ dạng (placeholder, **không** dùng chuỗi thật trong Git):

   ```text
   postgresql://USER:PASSWORD@ep-xxxx-pooler.region.aws.neon.tech/neondb?sslmode=require
   ```

   - Host có `-pooler` và cổng **6543** → pooled (dùng cho `DATABASE_URL` trên Render).
   - Chuỗi **direct** (cổng 5432, không pooler) dùng khi cần công cụ admin; app vẫn nên dùng pooled.

6. Lưu chuỗi vào password manager. **Không** dán vào README, issue public, hay frontend.

### Kiểm tra từ máy bạn (Windows PowerShell)

```powershell
cd D:\Downloads\club-attendance   # thư mục repo của bạn
.\.venv\Scripts\activate
pip install -r requirements-server.txt

$env:DATABASE_URL = "postgresql://..."   # dán chuỗi Neon pooled
python tools/migrate.py
```

Kết quả mong đợi: in `Không có migration mới` hoặc danh sách file migration vừa áp dụng; không lỗi kết nối.

Trong **Neon → SQL Editor** có thể chạy:

```sql
SELECT version, applied_at FROM schema_migrations ORDER BY version;
```

---

## B. Migration, import SQLite, backup

### Migration

- File: `migrations/001_init.sql`, `002_member_images.sql`, `003_enroll_sessions.sql`.
- Server **tự chạy** migration chưa có khi khởi động (bảng `schema_migrations`). Không DROP bảng.

Lệnh tay (giống trên): `python tools/migrate.py`.

### Nhập SQLite cũ (nếu có)

Repo **không** kèm `data/club.db`. Nếu bạn có file cũ từ bản HF/local:

```powershell
$env:DATABASE_URL = "postgresql://..."
python tools/import_sqlite.py --db D:\duong\dan\club.db --dry-run
python tools/import_sqlite.py --db D:\duong\dan\club.db
```

Dữ liệu cũ chỉ có vector, không có ảnh — bảng `member_images` có thể trống sau import.

### Backup

- UI: **Cài đặt → Tải sao lưu CSDL** → `club-backup.zip`.
- Neon: backup/PITR theo gói trên dashboard.

---

## C. Deploy lên Render (hướng dẫn chi tiết)

Có **hai cách** tạo dịch vụ; chọn một.

### Cách 1 — Blueprint (khuyến nghị nếu repo đã có `render.yaml`)

1. Đăng nhập [Render Dashboard](https://dashboard.render.com/).
2. **New +** → **Blueprint**.
3. Kết nối GitHub → chọn repository `club-attendance` (hoặc tên repo của bạn).
4. Render đọc `render.yaml` → preview service `club-attendance` (Docker, plan free).
5. Render sẽ hỏi giá trị cho biến `sync: false`:
   - **API_TOKEN** — tạo chuỗi ngẫu nhiên dài (ví dụ 32+ ký tự). **Không** dùng `dev` trên production.
   - **DATABASE_URL** — chuỗi Neon pooled.
   - **CORS_ORIGINS** — để trống nếu chỉ dùng UI trên cùng domain Render (`https://xxx.onrender.com/ui/`). Nếu UI trên Vercel: `https://ten-ban.vercel.app` (nhiều origin: phẩy, không khoảng thừa).
6. **Apply** → Render tạo Web Service và chạy deploy đầu tiên.

### Cách 2 — Tạo Web Service thủ công

1. **New +** → **Web Service**.
2. Connect repository GitHub (cùng repo).
3. Cấu hình:

   | Mục | Giá trị |
   |-----|---------|
   | Name | `club-attendance` (tuỳ ý; URL sẽ là `https://<name>.onrender.com`) |
   | Region | Gần bạn / gần Neon (vd Singapore nếu có) |
   | Branch | `main` |
   | Root Directory | *(để trống — repo gốc)* |
   | Runtime | **Docker** |
   | Dockerfile path | `./Dockerfile` |
   | Instance type | **Free** |

4. **Advanced** (nếu có):
   - **Health Check Path**: `/api/health`
   - Không bật persistent disk (app không lưu dữ liệu trên disk Render).

5. **Environment Variables** — thêm từng dòng:

   | Key | Secret? | Giá trị |
   |-----|---------|---------|
   | `API_TOKEN` | Có | chuỗi mạnh của bạn |
   | `DATABASE_URL` | Có | Neon pooled + `sslmode=require` |
   | `APP_ENV` | Không | `production` |
   | `TZ_NAME` | Không | `Asia/Ho_Chi_Minh` |
   | `MODEL_NAME` | Không | **`buffalo_s`** trên Free; `buffalo_l` cần RAM lớn hơn |
   | `SKIP_MODEL_WARMUP` | Không | `1` trên Render Free |
   | `FACE_MAX_CONCURRENCY` | Không | `1` trên Render Free |
   | `INSIGHTFACE_HOME` | Không | `/home/user/.insightface` |
   | `CORS_ORIGINS` | Không | *(trống hoặc origin UI)* |

   **Không** cần tự đặt `PORT` — Render inject khi container chạy.

6. **Create Web Service** → bắt đầu build.

### Trong lúc build và deploy

1. Tab **Logs** → **Build logs**:
   - Cài Python deps, tải InsightFace (mặc định image: **`buffalo_s`**) — **5–15 phút** là bình thường.
   - Build fail thường do: hết thời gian build Free, lỗi mạng khi tải model, thiếu thư viện hệ thống (Dockerfile repo đã cài `libgl1`, `g++`, …).

2. Tab **Logs** → **Deploy logs** (runtime):
   - Dòng kiểu `Kết nối database postgresql://host.../db` (không in password).
   - `Sẵn sàng: ... mẫu khuôn mặt` sau khi model warm-up.
   - Nếu thiếu `API_TOKEN` hoặc `DATABASE_URL`: process thoát ngay — sửa Environment → **Save** → **Manual Deploy**.

3. Khi trạng thái **Live**, URL public:

   ```text
   https://club-attendance.onrender.com
   ```

   (tên phụ thuộc **Name** bạn đặt).

### Sau khi Live — dùng lần đầu

1. Mở trình duyệt: `https://<service>.onrender.com/ui/`
2. Nếu service vừa ngủ: trang có thể **chậm 30–90 giây** lần đầu (cold start).
3. Bấm **Token** → nhập đúng `API_TOKEN` đã đặt trên Render → Lưu.
4. **Cài đặt → Kiểm tra kết nối**: phải thấy model, số thành viên, `db=true` trong health (nếu ping qua API).
5. **Thành viên** → thêm người → **Đăng ký mặt** (webcam cần HTTPS — Render đã có).

### Kiểm tra tự động từ máy bạn

```powershell
$env:DATABASE_URL = "postgresql://..."   # cùng chuỗi đã đặt trên Render
python tools/smoke_test.py https://club-attendance.onrender.com --token YOUR_API_TOKEN --db-url $env:DATABASE_URL
```

Kết quả mong đợi: dòng cuối `SMOKE OK`.

Kiểm tra cấu hình repo (không cần secret):

```powershell
python tools/deploy_render.py --check
```

### Cập nhật code sau này

- Push lên GitHub branch đã liên kết → Render **auto-deploy** (nếu bật Auto-Deploy, mặc định thường bật).
- Hoặc Dashboard → service → **Manual Deploy → Deploy latest commit**.
- Tuỳ chọn: tạo [Render API Key](https://dashboard.render.com/u/settings#api-keys), đặt `RENDER_SERVICE_ID` (tab Settings → ID dịch vụ), rồi:

  ```powershell
  $env:RENDER_API_KEY = "rnd_..."
  python tools/deploy_render.py --deploy --service-id srv-xxxxx
  ```

  Script **không** tạo service mới hay nâng plan trả phí.

### Client Python (cam cửa / cam trần) trỏ Render

```powershell
$env:SERVER_URL = "https://club-attendance.onrender.com"
$env:API_TOKEN = "YOUR_API_TOKEN"
pip install -r requirements-client.txt
python client\door_client.py --source 0 --show
```

Không cần `HF_TOKEN` (chỉ dùng cho Space private cũ).

### UI trên Vercel, API trên Render

1. Render: đặt `CORS_ORIGINS=https://ten-ban.vercel.app`
2. Build UI: `python deploy/vercel/build.py --api-base https://club-attendance.onrender.com` hoặc proxy `BACKEND_URL` trong `deploy/vercel` (xem README).
3. Trên Vercel **không** đặt `DATABASE_URL` — chỉ backend Render giữ secret DB.

### Camera / HTTPS

Webcam trình duyệt chỉ chạy trên `localhost` hoặc **HTTPS**. URL `*.onrender.com` là HTTPS.

---

## D. Xử lý lỗi thường gặp trên Render

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|-------------|------------------------|------------|
| Build timeout / failed | Free tier, tải model lâu | Xem build log; deploy lại; tránh commit làm rebuild không cần thiết |
| Deploy crash ngay khi start | Thiếu `API_TOKEN` / `DATABASE_URL` | Environment → thêm secret → Save → Manual Deploy |
| `API_TOKEN quá yếu` | `APP_ENV=production` + token `dev` | Đổi token mạnh trên Render |
| `/api/health` 503, `db: false` | Sai `DATABASE_URL`, Neon suspend, hết compute | Kiểm tra Neon dashboard; dùng pooled URL; mở project Neon |
| **Out of memory (512Mi)** / crash trước khi mở port | `buffalo_l` + warmup quá nặng | Trên Render Dashboard đặt `MODEL_NAME=buffalo_s`, `SKIP_MODEL_WARMUP=1`, `FACE_MAX_CONCURRENCY=1`; push code mới (Docker preload `buffalo_s`); **Manual Deploy** |
| 502 / timeout khi đăng ký | RAM Free thấp, request lớn | Giảm kích thước khung; tránh upload quá nhiều ảnh một lần; thử lại sau cold start |
| Trang trắng / 404 UI | Sai URL | Dùng `/ui/` hoặc `/` (redirect sang UI) |
| 401 mọi API | Token UI ≠ `API_TOKEN` Render | Đồng bộ token trong dialog Token |
| CORS từ Vercel | Thiếu `CORS_ORIGINS` | Thêm đúng origin (không slash cuối) |
| Webcam không bật | Mở HTTP không phải localhost | Chỉ dùng URL Render HTTPS |

Xem log runtime: Dashboard → service → **Logs** (filter Errors). Không paste log có secret lên nơi công khai.

---

## E. Vận hành hàng ngày

### Cold start

Free sleep sau ~15 phút không traffic. Request đầu có thể 30–90 giây. Smoke test và UI đã có gợi ý chờ health.

### CPU / RAM

InsightFace chạy CPU. Một worker (`--workers 1` trong Dockerfile). Không scale nhiều instance Free cho cùng app (phiên/model trong một process).

### Neon

- Dùng URL **pooler** cho `DATABASE_URL`.
- Tuỳ chọn: `DB_POOL_MAX=4` (Render Environment).
- Theo dõi storage: SQL Editor → kích thước bảng `member_images`, `embeddings`.

### Ảnh và quyền riêng tư

Chỉ lưu JPEG mẫu đăng ký (BYTEA). Check-in không lưu ảnh. Ảnh tải qua `GET /api/members/{id}/images/{img_id}` + token. DB không mã hóa E2E.

### Phiên đăng ký / điểm danh

Phiên đăng ký TTL 20 phút trong PostgreSQL. Hiện diện + handoff trong `kv`. Check-in trùng khi đang trong phòng → `already`.

---

## Biến môi trường (tóm tắt)

Xem `.env.example`.

| Biến | Bắt buộc trên Render | Ghi chú |
|------|----------------------|---------|
| `API_TOKEN` | Có | Secret; production không dùng `dev` |
| `DATABASE_URL` | Có | Neon pooled |
| `APP_ENV` | Khuyến nghị `production` | |
| `TZ_NAME` | Không | Mặc định blueprint: `Asia/Ho_Chi_Minh` |
| `MODEL_NAME` | Không | `buffalo_s` (Free) / `buffalo_l` (RAM đủ) |
| `SKIP_MODEL_WARMUP` | Khuyến nghị `1` trên Free | |
| `CORS_ORIGINS` | Khi UI khác domain | |
| `PORT` | Không (Render tự gán) | |

Local dev: `APP_ENV=local`, `PORT=7860`, cùng `DATABASE_URL` Neon hoặc Neon branch dev.

---

## Giới hạn Free (tự kiểm tra dashboard)

- **Render Free**: sleep, giới hạn build minutes/tháng, RAM ~512MB, không persistent disk.
- **Neon Free**: giới hạn storage và compute; project có thể auto-suspend.
- Model mặc định trong **Docker image**: `buffalo_s` (phù hợp Free). Server mở cổng trước, tải model **nền**; `/api/health` trả `ok` khi DB OK, `model_ready` khi model xong.

---

## Tài liệu liên quan

- [README.md](../README.md) — chạy local nhanh
- [docs/02-huong-dan-trien-khai.md](02-huong-dan-trien-khai.md) — quy trình CLB sau khi server đã chạy
- [docs/05-api-va-cau-hinh.md](05-api-va-cau-hinh.md) — API và biến môi trường
- [docs/06-xu-ly-su-co.md](06-xu-ly-su-co.md) — lỗi ứng dụng (nhận diện, camera, …)
