-- Alerts and drafts the system raises by itself. Loaded by app.py after schema.sql; safe to run repeatedly.
-- Nothing here is a balance: PO received/open kg and PO "received" status are derived from GRNs, like yarn stock.

-- One live (open or acked) alert per machine+shift or per operator. A later QC check updates it or clears it.
CREATE TABLE IF NOT EXISTS defect_alerts (
  id         INTEGER PRIMARY KEY,
  scope      TEXT NOT NULL CHECK (scope IN ('machine_shift', 'operator')),
  key        TEXT NOT NULL,                       -- "m4:s2" or "w7", what makes an alert unique
  machine_id INTEGER REFERENCES machines(id),
  shift_id   INTEGER REFERENCES shifts(id),
  worker_id  INTEGER REFERENCES workers(id),
  label      TEXT NOT NULL,                       -- "Loom 4 / Night" or the operator's name
  checks     INTEGER NOT NULL,
  defects    INTEGER NOT NULL,
  rate       REAL NOT NULL,
  baseline   REAL NOT NULL,                       -- defect rate everywhere else, or the assumed normal
  top_defect TEXT,
  top_stage  TEXT,
  suggestion TEXT NOT NULL,                       -- the maintenance note, written by code
  status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acked', 'cleared')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  acked_by   INTEGER REFERENCES users(id)
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_defect_alert_live ON defect_alerts (key) WHERE status IN ('open', 'acked');

-- Supplier purchase orders. auto = 1 means the system drafted it from a shortfall.
CREATE TABLE IF NOT EXISTS purchase_orders (
  id          INTEGER PRIMARY KEY,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'cancelled')),
  auto        INTEGER NOT NULL DEFAULT 0,
  note        TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  approved_by INTEGER REFERENCES users(id),
  approved_at TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS po_lines (
  id           INTEGER PRIMARY KEY,
  po_id        INTEGER NOT NULL REFERENCES purchase_orders(id),
  yarn_type_id INTEGER NOT NULL REFERENCES yarn_types(id),
  qty_kg       REAL NOT NULL CHECK (qty_kg > 0),
  rate         REAL NOT NULL DEFAULT 0 CHECK (rate >= 0),
  reason       TEXT,
  locked       INTEGER NOT NULL DEFAULT 0,        -- 1 once a person edited it: the system stops recomputing it
  UNIQUE (po_id, yarn_type_id)
) STRICT;
-- A GRN received against a PO line. The GRN itself is unchanged.
CREATE TABLE IF NOT EXISTS po_receipts (
  id         INTEGER PRIMARY KEY,
  po_line_id INTEGER NOT NULL REFERENCES po_lines(id),
  grn_id     INTEGER NOT NULL UNIQUE REFERENCES grn(id)
) STRICT;

-- One owner digest per day: the numbers (JSON) and the sentence written from them.
CREATE TABLE IF NOT EXISTS digests (
  digest_date TEXT PRIMARY KEY,
  data        TEXT NOT NULL,
  summary     TEXT NOT NULL,
  source      TEXT NOT NULL CHECK (source IN ('ai', 'template')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

-- The chat assistant (web and WhatsApp): one row per conversation that ends in one request.
CREATE TABLE IF NOT EXISTS bot_conversations (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  channel    TEXT NOT NULL CHECK (channel IN ('web', 'whatsapp')),
  peer       TEXT NOT NULL DEFAULT '',          -- the WhatsApp number; '' on the web
  status     TEXT NOT NULL DEFAULT 'collecting'
             CHECK (status IN ('collecting', 'confirming', 'done', 'failed', 'cancelled', 'closed')),
  messages   TEXT NOT NULL DEFAULT '[]',        -- [{"from": "user" | "bot", "text": ...}]
  skill      TEXT,                              -- the skill Jev picked; 'plan' for several steps in one message
  difficulty TEXT,                              -- Jev's rating of the message: easy / medium / hard
  model      TEXT,                              -- the chat model used for it (picked by difficulty)
  draft      TEXT,                              -- {"skill", "values", "names", "summary"} once complete; {"choices"} when asking which
  jev        REAL,                              -- Jev's probability that the request matches the conversation
  result     TEXT,                              -- what the system answered when it was run
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
