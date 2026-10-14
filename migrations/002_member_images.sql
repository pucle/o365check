-- Ảnh khuôn mặt lưu BYTEA, gắn với thành viên qua img_id (cùng giá trị embeddings.img).
CREATE TABLE IF NOT EXISTS member_images (
  id BIGSERIAL PRIMARY KEY,
  member_id TEXT NOT NULL REFERENCES members (id) ON DELETE CASCADE,
  img_id TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT 'image/jpeg',
  byte_size INTEGER NOT NULL,
  created TEXT,
  data BYTEA NOT NULL,
  UNIQUE (member_id, img_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_member_images_img_id ON member_images (img_id);
CREATE INDEX IF NOT EXISTS idx_member_images_member ON member_images (member_id);
