CREATE TABLE IF NOT EXISTS items (
  id             INTEGER PRIMARY KEY,
  source_id      TEXT NOT NULL,
  org            TEXT NOT NULL,
  country        TEXT,
  region         TEXT,
  target_name    TEXT,
  url            TEXT NOT NULL,
  canonical_url  TEXT NOT NULL UNIQUE,
  title          TEXT,
  author         TEXT,
  published_at   TEXT,
  date_source    TEXT,
  first_seen     TEXT NOT NULL,
  last_seen      TEXT NOT NULL,
  updated_at     TEXT,
  body_text      TEXT,
  summary        TEXT,
  word_count     INTEGER,
  content_hash   TEXT,
  media_type     TEXT,
  language       TEXT,
  content_type   TEXT,
  topics         TEXT,
  pub_content_type TEXT,   -- the publisher's own label, verbatim
  pub_topics       TEXT,   -- json array of the publisher's own topics

  confidence     REAL,
  needs_review   INTEGER DEFAULT 0,
  reviewed       INTEGER DEFAULT 0,
  duplicate_of   INTEGER,
  status         TEXT NOT NULL,
  error          TEXT,
  run_id         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_hash   ON items(content_hash);
CREATE INDEX IF NOT EXISTS idx_items_seen   ON items(first_seen);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_id);

CREATE TABLE IF NOT EXISTS revisions (
  id           INTEGER PRIMARY KEY,
  item_id      INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  seen_at      TEXT NOT NULL,
  word_count   INTEGER,
  UNIQUE(item_id, content_hash)
);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  discovered  INTEGER DEFAULT 0,
  extracted   INTEGER DEFAULT 0,
  duplicates  INTEGER DEFAULT 0,
  classified  INTEGER DEFAULT 0,
  ok          INTEGER DEFAULT 1,
  notes       TEXT
);

CREATE TABLE IF NOT EXISTS source_runs (
  id        INTEGER PRIMARY KEY,
  run_id    INTEGER NOT NULL,
  source_id TEXT NOT NULL,
  target    TEXT,
  found     INTEGER DEFAULT 0,
  new_items INTEGER DEFAULT 0,
  ok        INTEGER DEFAULT 1,
  error     TEXT
);
