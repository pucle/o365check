# Quyền riêng tư & giấy phép

* **Dữ liệu sinh trắc.** Vector mặt là dữ liệu nhạy cảm; theo quy định bảo vệ dữ liệu cá nhân (ví dụ Nghị định 13/2023/NĐ-CP tại Việt Nam) cần thông báo mục đích và **có sự đồng ý rõ ràng** của từng thành viên, cho phép rút lại (xoá người = xoá toàn bộ mẫu). Đây không phải tư vấn pháp lý.
* Lưu vector embedding và **ảnh mẫu đăng ký (JPEG BYTEA)** trên Neon. Ảnh check-in không lưu. Truy cập ảnh qua API đã xác thực. Database **không** đồng nghĩa mã hóa đầu cuối. Render/Neon để private; token mạnh; hạn chế người có quyền; dán thông báo camera ở cửa. Không log ảnh/embedding/DSN.
* **Giấy phép model**: mã nguồn InsightFace theo MIT nhưng **trọng số `buffalo_l` chỉ cho nghiên cứu phi thương mại**. Dùng nội bộ CLB sinh viên không thu phí thường hợp lý, nhưng hãy tự đọc điều khoản tại github.com/deepinsight/insightface; nếu thương mại hoá cần model có giấy phép phù hợp.
* Hệ thống không phải biện pháp an ninh: không dùng để mở khoá cửa hay xử phạt chỉ dựa trên kết quả tự động; luôn có cách sửa tay.
