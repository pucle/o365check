-- Phiên đăng ký bền vững (sống sót qua restart Render). Ảnh/embedding ứng viên chỉ tồn tại đến khi commit hoặc hết TTL.
CREATE TABLE IF NOT EXISTS enroll_sessions (
  sid TEXT PRIMARY KEY,
  member_id TEXT NOT NULL,
  name TEXT,
  mode TEXT NOT NULL,
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  created DOUBLE PRECISION NOT NULL,
  touched DOUBLE PRECISION NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  frames INTEGER NOT NULL DEFAULT 0,
  rejects TEXT NOT NULL DEFAULT '{}',
  pending_report TEXT,
  pending_replace BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_enroll_sessions_touched ON enroll_sessions (touched);

CREATE TABLE IF NOT EXISTS enroll_candidates (
  sid TEXT NOT NULL REFERENCES enroll_sessions (sid) ON DELETE CASCADE,
  fid INTEGER NOT NULL,
  dir TEXT,
  pitch DOUBLE PRECISION,
  yaw DOUBLE PRECISION,
  det DOUBLE PRECISION,
  width INTEGER,
  blur DOUBLE PRECISION,
  light DOUBLE PRECISION,
  clipped BOOLEAN,
  nfaces INTEGER,
  emb BYTEA,
  kps BYTEA,
  bbox TEXT,
  jpg BYTEA,
  PRIMARY KEY (sid, fid)
);

CREATE TABLE IF NOT EXISTS enroll_pending_rows (
  sid TEXT NOT NULL REFERENCES enroll_sessions (sid) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  emb BYTEA NOT NULL,
  pitch DOUBLE PRECISION,
  yaw DOUBLE PRECISION,
  img TEXT,
  step TEXT,
  PRIMARY KEY (sid, ord)
);
