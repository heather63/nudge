-- The Google Sheet is the source of truth. SQLite is a mirror plus everything
-- scheduling-related, because a sheet cannot express cooldowns or offer history.
-- One row in the sheet = one assignment = one task in Google Tasks.

CREATE TABLE IF NOT EXISTS assignments (
  id              INTEGER PRIMARY KEY,
  sheet_id        TEXT UNIQUE,                      -- stable id, column in the sheet
  name            TEXT NOT NULL,
  course          TEXT,                             -- "class" column / Tasks list
  kind            TEXT,                             -- "type" column: hw, exam, project...
  due_at          TEXT,                             -- ISO8601, may be NULL
  expected_min    INTEGER,                          -- "expected length total"
  percent_done    REAL NOT NULL DEFAULT 0,          -- 0-100
  files           TEXT,                             -- "relevant files" column
  repeat_rule     TEXT,                             -- "repeats" column, e.g. weekly:fri
  not_before      TEXT,                             -- "not before" column, e.g. fri 10am
  available_from  TEXT,                             -- resolved from not_before + due_at
  gtask_id        TEXT,
  gtasklist_id    TEXT,
  status          TEXT NOT NULL DEFAULT 'active',   -- active|done|dropped
  created_at      TEXT NOT NULL,
  sheet_synced_at TEXT,                             -- local change pushed to the sheet
  synced_at       TEXT                              -- local change pushed to Tasks
);

CREATE TABLE IF NOT EXISTS assignment_state (
  assignment_id   INTEGER PRIMARY KEY REFERENCES assignments(id),
  last_offered_at TEXT,
  offer_count     INTEGER NOT NULL DEFAULT 0,
  decline_count   INTEGER NOT NULL DEFAULT 0,
  snooze_until    TEXT
);

CREATE TABLE IF NOT EXISTS documents (
  id              INTEGER PRIMARY KEY,
  assignment_id   INTEGER NOT NULL REFERENCES assignments(id),
  path            TEXT NOT NULL UNIQUE,
  sha256          TEXT NOT NULL,
  text_cache      TEXT,
  ingested_at     TEXT
);

-- The audit log. This is what lets the scoring weights be tuned against what
-- actually happened rather than against intuition.
CREATE TABLE IF NOT EXISTS offers (
  id              INTEGER PRIMARY KEY,
  assignment_id   INTEGER NOT NULL REFERENCES assignments(id),
  offered_at      TEXT NOT NULL,
  gap_start       TEXT NOT NULL,
  gap_end         TEXT NOT NULL,
  session_min     INTEGER NOT NULL,                 -- how long nudge asked for
  response        TEXT,                             -- done|progress|not_now|snooze|skip|ignored
  minutes_logged  REAL,                             -- what actually got done
  responded_at    TEXT
);

-- LLM-proposed expected length / type / due date, awaiting approval.
CREATE TABLE IF NOT EXISTS proposals (
  id              INTEGER PRIMARY KEY,
  assignment_id   INTEGER NOT NULL REFERENCES assignments(id),
  payload_json    TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  resolved_at     TEXT,
  resolution      TEXT                              -- approved|rejected|superseded
);

CREATE TABLE IF NOT EXISTS kv (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assignments_gtask ON assignments(gtask_id);
CREATE INDEX IF NOT EXISTS idx_assignments_status ON assignments(status);
CREATE INDEX IF NOT EXISTS idx_offers_offered_at ON offers(offered_at DESC);
CREATE INDEX IF NOT EXISTS idx_offers_assignment ON offers(assignment_id, offered_at DESC);
