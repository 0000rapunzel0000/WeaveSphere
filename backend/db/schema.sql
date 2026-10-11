-- Everest Textile Mills. Money in rupees (REAL), quantities in kg / metres.

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'store', 'supervisor', 'qc')),
  email TEXT,
  phone TEXT,
  active INTEGER NOT NULL DEFAULT 1 -- 0 = removed: can't sign in; history keeps the name
) STRICT;

-- Sign-in uses signed JWTs, so there is no session table: just the signing key and signed-out tokens until they expire.
DROP TABLE IF EXISTS sessions;
CREATE TABLE IF NOT EXISTS app_secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS revoked_tokens (jti TEXT PRIMARY KEY, expires_at INTEGER NOT NULL) STRICT;

-- Master data

CREATE TABLE IF NOT EXISTS buyers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, email TEXT, gstin TEXT, address TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, gstin TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS yarn_types (
  id INTEGER PRIMARY KEY,
  count TEXT NOT NULL, material TEXT NOT NULL, colour TEXT NOT NULL,
  reorder_kg REAL NOT NULL DEFAULT 0 CHECK (reorder_kg >= 0),
  UNIQUE (count, material, colour)
) STRICT;

CREATE TABLE IF NOT EXISTS fabric_types (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, hsn TEXT,
  gst_rate REAL NOT NULL DEFAULT 5 CHECK (gst_rate >= 0)
) STRICT;

CREATE TABLE IF NOT EXISTS bom (
  id INTEGER PRIMARY KEY,
  fabric_type_id INTEGER NOT NULL REFERENCES fabric_types,
  yarn_type_id INTEGER NOT NULL REFERENCES yarn_types,
  kg_per_m REAL NOT NULL CHECK (kg_per_m > 0),
  UNIQUE (fabric_type_id, yarn_type_id)
) STRICT;

CREATE TABLE IF NOT EXISTS machines (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('spinning', 'weaving', 'dyeing')),
  status TEXT NOT NULL DEFAULT 'Idle' CHECK (status IN ('Running', 'Idle', 'Breakdown', 'Maintenance')),
  overhead_per_hour REAL NOT NULL DEFAULT 0 CHECK (overhead_per_hour >= 0),
  rate_m_per_hour REAL NOT NULL DEFAULT 0 CHECK (rate_m_per_hour >= 0) -- running speed; 0 = no live output
) STRICT;

CREATE TABLE IF NOT EXISTS workers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT,
  wage_type TEXT NOT NULL CHECK (wage_type IN ('daily', 'piece')),
  rate REAL NOT NULL CHECK (rate >= 0), -- per day, or per unit of output
  active INTEGER NOT NULL DEFAULT 1 -- 0 = left the mill: hidden from pickers; history keeps the name
) STRICT;

CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, starts TEXT, ends TEXT
) STRICT;

-- Raw material

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  buyer_id INTEGER NOT NULL REFERENCES buyers,
  fabric_type_id INTEGER NOT NULL REFERENCES fabric_types,
  colour TEXT NOT NULL,
  qty_m REAL NOT NULL CHECK (qty_m > 0),
  deadline TEXT NOT NULL,
  price_per_m REAL NOT NULL CHECK (price_per_m >= 0),
  token TEXT NOT NULL UNIQUE, -- buyer tracking link
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS grn (
  id INTEGER PRIMARY KEY,
  supplier_id INTEGER NOT NULL REFERENCES suppliers,
  yarn_type_id INTEGER NOT NULL REFERENCES yarn_types,
  qty_kg REAL NOT NULL CHECK (qty_kg > 0),
  rate REAL NOT NULL CHECK (rate >= 0),
  order_id INTEGER REFERENCES orders,
  challan_no TEXT,
  received_on TEXT NOT NULL DEFAULT CURRENT_DATE,
  created_by INTEGER REFERENCES users
) STRICT;

CREATE TABLE IF NOT EXISTS material_issues (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders,
  yarn_type_id INTEGER NOT NULL REFERENCES yarn_types,
  machine_id INTEGER REFERENCES machines,
  qty_kg REAL NOT NULL CHECK (qty_kg > 0),
  rate REAL, -- moving-average cost per kg at the moment of issue, so closed months never change
  issued_on TEXT NOT NULL DEFAULT CURRENT_DATE,
  created_by INTEGER REFERENCES users
) STRICT;

-- Yarn that left the store without going to an order: damaged, wasted, or a physical count that came up short.
CREATE TABLE IF NOT EXISTS stock_writeoffs (
  id INTEGER PRIMARY KEY,
  yarn_type_id INTEGER NOT NULL REFERENCES yarn_types,
  qty_kg REAL NOT NULL CHECK (qty_kg > 0),
  reason TEXT NOT NULL CHECK (reason IN ('damaged', 'wastage', 'count_correction', 'returned_to_supplier', 'other')),
  note TEXT,
  rate REAL, -- moving-average cost per kg at the moment of write-off: what the loss is worth
  written_on TEXT NOT NULL DEFAULT CURRENT_DATE,
  created_by INTEGER REFERENCES users
) STRICT;

-- Stock is derived, never stored, so it cannot drift from GRNs and issues. The covering indexes below
-- keep it fast: 60 yarns over 500k GRNs + 2M issues read in ~80 ms (4 s without them).
-- avg_rate is the moving average of what is on hand: (value received - value issued) / kg on hand.
-- ponytail: moving-average costing (allowed under Ind AS 2); FIFO needs an issue -> GRN allocation table.
DROP VIEW IF EXISTS yarn_stock;
CREATE VIEW yarn_stock AS
SELECT *, CASE WHEN stock_kg > 0.001 THEN (grn_value - issued_value) / stock_kg END AS avg_rate
FROM (SELECT y.*,
  COALESCE((SELECT SUM(qty_kg) FROM grn WHERE yarn_type_id = y.id), 0)
    - COALESCE((SELECT SUM(qty_kg) FROM material_issues WHERE yarn_type_id = y.id), 0)
    - COALESCE((SELECT SUM(qty_kg) FROM stock_writeoffs WHERE yarn_type_id = y.id), 0) AS stock_kg,
  COALESCE((SELECT SUM(qty_kg * rate) FROM grn WHERE yarn_type_id = y.id), 0) AS grn_value,
  COALESCE((SELECT SUM(qty_kg * rate) FROM material_issues WHERE yarn_type_id = y.id), 0)
    + COALESCE((SELECT SUM(qty_kg * COALESCE(rate, 0)) FROM stock_writeoffs WHERE yarn_type_id = y.id), 0) AS issued_value
  FROM yarn_types y);

-- Production floor

CREATE TABLE IF NOT EXISTS job_cards ( -- stage = machine.kind
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders,
  machine_id INTEGER NOT NULL REFERENCES machines,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  operator_id INTEGER REFERENCES workers, -- who live output is credited to
  shift_id INTEGER REFERENCES shifts,
  running_since TEXT, -- UTC; when the machine last started running this job
  last_tick TEXT,     -- UTC; live output has been counted up to here (NULL while stopped)
  released INTEGER NOT NULL DEFAULT 0 -- 1 = its operator was moved off it: never credit the machine's last operator
) STRICT;

CREATE TABLE IF NOT EXISTS machine_status_log (
  id INTEGER PRIMARY KEY,
  machine_id INTEGER NOT NULL REFERENCES machines,
  status TEXT NOT NULL,
  note TEXT,
  category TEXT, severity TEXT, assignee TEXT, -- breakdown triage
  at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS attendance (
  id INTEGER PRIMARY KEY,
  worker_id INTEGER NOT NULL REFERENCES workers,
  shift_id INTEGER NOT NULL REFERENCES shifts,
  work_date TEXT NOT NULL,
  UNIQUE (worker_id, work_date, shift_id)
) STRICT;

CREATE TABLE IF NOT EXISTS production_entries (
  id INTEGER PRIMARY KEY,
  entry_date TEXT NOT NULL,
  shift_id INTEGER NOT NULL REFERENCES shifts,
  machine_id INTEGER NOT NULL REFERENCES machines,
  order_id INTEGER NOT NULL REFERENCES orders,
  operator_id INTEGER NOT NULL REFERENCES workers,
  output REAL NOT NULL CHECK (output >= 0), -- metres (kg for spinning)
  hours REAL NOT NULL DEFAULT 0 CHECK (hours >= 0),
  auto INTEGER NOT NULL DEFAULT 0 -- 1 = counted live from a running machine
) STRICT;

CREATE TABLE IF NOT EXISTS rolls (
  id INTEGER PRIMARY KEY,
  qr TEXT NOT NULL UNIQUE,
  order_id INTEGER NOT NULL REFERENCES orders,
  machine_id INTEGER NOT NULL REFERENCES machines,
  shift_id INTEGER NOT NULL REFERENCES shifts,
  operator_id INTEGER NOT NULL REFERENCES workers,
  metres REAL NOT NULL CHECK (metres > 0),
  status TEXT NOT NULL DEFAULT 'pending_qc'
    CHECK (status IN ('pending_qc', 'passed', 'rework', 'rejected', 'dispatched')),
  made_on TEXT NOT NULL DEFAULT CURRENT_DATE
) STRICT;

CREATE TABLE IF NOT EXISTS qc_records (
  id INTEGER PRIMARY KEY,
  roll_id INTEGER NOT NULL REFERENCES rolls,
  result TEXT NOT NULL CHECK (result IN ('pass', 'rework', 'reject')),
  defect_type TEXT CHECK (defect_type IN ('hole', 'colour_mismatch', 'uneven_weave', 'stain', 'other')),
  severity TEXT, stage_at_fault TEXT,
  notes TEXT,
  inspector_id INTEGER REFERENCES users,
  at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

-- Dispatch & accounts

CREATE TABLE IF NOT EXISTS dispatches (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders,
  vehicle_no TEXT NOT NULL,
  driver TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE IF NOT EXISTS dispatch_items (
  id INTEGER PRIMARY KEY,
  dispatch_id INTEGER NOT NULL REFERENCES dispatches,
  roll_id INTEGER NOT NULL UNIQUE REFERENCES rolls -- a roll goes on one truck, ever
) STRICT;

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY,
  dispatch_id INTEGER NOT NULL UNIQUE REFERENCES dispatches,
  order_id INTEGER NOT NULL REFERENCES orders,
  metres REAL NOT NULL, rate REAL NOT NULL,
  subtotal REAL NOT NULL, gst_rate REAL NOT NULL, gst REAL NOT NULL, total REAL NOT NULL,
  issued_on TEXT NOT NULL DEFAULT CURRENT_DATE
) STRICT;

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  party TEXT NOT NULL CHECK (party IN ('buyer', 'supplier')),
  party_id INTEGER NOT NULL,
  invoice_id INTEGER REFERENCES invoices,
  amount REAL NOT NULL CHECK (amount > 0),
  paid_on TEXT NOT NULL DEFAULT CURRENT_DATE,
  ref TEXT
) STRICT;

-- AI

CREATE TABLE IF NOT EXISTS buyer_messages (
  id INTEGER PRIMARY KEY,
  buyer_id INTEGER REFERENCES buyers,
  order_id INTEGER REFERENCES orders,
  channel TEXT NOT NULL DEFAULT 'whatsapp',
  text TEXT NOT NULL,
  intent TEXT, dept TEXT, urgency TEXT, confidence REAL,
  status TEXT NOT NULL DEFAULT 'review' CHECK (status IN ('review', 'routed', 'replied')),
  reply TEXT,
  external_id TEXT,  -- e.g. the voice platform's call id, so a retried webhook logs the call once
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_external ON buyer_messages (external_id);

-- Every Jev decision. Open rows are the human review queue; auto-applied rows can still be corrected,
-- and the corrections measure how often Jev is right at each confidence level.
CREATE TABLE IF NOT EXISTS ai_reviews (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('message', 'breakdown', 'defect')),
  ref_id INTEGER NOT NULL,
  suggestion TEXT NOT NULL, -- JSON
  confidence REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
  auto INTEGER NOT NULL DEFAULT 0, -- 1 = applied without a person
  final TEXT, -- JSON of what a person confirmed or corrected; NULL = nobody has checked it
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE INDEX IF NOT EXISTS ix_rolls_order ON rolls (order_id, status);
CREATE INDEX IF NOT EXISTS ix_production_order ON production_entries (order_id);
CREATE INDEX IF NOT EXISTS ix_production_machine_date ON production_entries (machine_id, entry_date);
CREATE INDEX IF NOT EXISTS ix_jobs_order ON job_cards (order_id);
CREATE INDEX IF NOT EXISTS ix_grn_yarn ON grn (yarn_type_id, qty_kg, rate);
CREATE INDEX IF NOT EXISTS ix_issues_yarn ON material_issues (yarn_type_id, qty_kg, rate);
CREATE INDEX IF NOT EXISTS ix_issues_order ON material_issues (order_id, yarn_type_id);
CREATE INDEX IF NOT EXISTS ix_ai_reviews_status ON ai_reviews (status);
