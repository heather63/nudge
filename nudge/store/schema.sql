-- Assignments live in the Google Sheet, not here. SQLite only holds what a
-- sheet can't: scheduling state and the offers log, keyed by the row's ID.

CREATE TABLE IF NOT EXISTS state (
  sheet_id        TEXT PRIMARY KEY,
  snooze_until    TEXT,
  decline_count   INTEGER NOT NULL DEFAULT 0      -- consecutive "not now"s
);

-- The audit log: what was offered, and what you said.
CREATE TABLE IF NOT EXISTS offers (
  id              INTEGER PRIMARY KEY,
  sheet_id        TEXT NOT NULL,
  label           TEXT NOT NULL,                  -- "Class · Assignment" at the time
  offered_at      TEXT NOT NULL,
  gap_start       TEXT NOT NULL,
  gap_end         TEXT NOT NULL,
  session_min     INTEGER NOT NULL,               -- how long nudge asked for
  response        TEXT,                           -- done|progress|not_now|other|snooze|ignored
  minutes_logged  REAL,                           -- what actually got done
  responded_at    TEXT
);

CREATE TABLE IF NOT EXISTS kv (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_offers_offered_at ON offers(offered_at DESC);
