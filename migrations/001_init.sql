-- Schema gốc (SQLite → PostgreSQL). Không DROP bảng.
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
  id BIGSERIAL PRIMARY KEY,
  member_id TEXT NOT NULL,
  vec BYTEA NOT NULL,
  source TEXT,
  pitch DOUBLE PRECISION,
  yaw DOUBLE PRECISION,
  created TEXT,
  img TEXT,
  step TEXT
);
CREATE INDEX IF NOT EXISTS idx_embeddings_member ON embeddings (member_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_img ON embeddings (img);

CREATE TABLE IF NOT EXISTS strangers (
  id BIGSERIAL PRIMARY KEY,
  vec BYTEA NOT NULL,
  pitch DOUBLE PRECISION,
  yaw DOUBLE PRECISION,
  context TEXT,
  created TEXT
);

CREATE TABLE IF NOT EXISTS shifts (
  id BIGSERIAL PRIMARY KEY,
  member_id TEXT NOT NULL,
  start TEXT NOT NULL,
  "end" TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shifts_start ON shifts (start);

CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  member_id TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_member ON events (member_id);

CREATE TABLE IF NOT EXISTS absences (
  id BIGSERIAL PRIMARY KEY,
  member_id TEXT NOT NULL,
  start TEXT NOT NULL,
  "end" TEXT NOT NULL,
  minutes DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_absences_member_start ON absences (member_id, start);

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
