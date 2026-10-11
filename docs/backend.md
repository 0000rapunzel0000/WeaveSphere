# Everest Textile Mills: Backend API

This is the reference for building the frontend. The live, clickable schema for every endpoint is at **`http://localhost:8000/docs`**.

## Running it

```bash
cd backend
pip install -r requirements.txt
python3 seed.py                   # optional: demo data + one account per role (owner, admin, store, floor, qc)
uvicorn app:app --reload          # http://localhost:8000
```

```bash
cd frontend
npm install
npm run dev                       # http://localhost:5173, proxies /api/* to the backend on :8000
```

- The database is a single SQLite file, `backend/mill.db`, wherever you start the server from (`DB_PATH` overrides it). To reset everything, delete it. `seed.py` only runs on an empty database and prints the demo password (`DEMO_PASSWORD` to choose it).
- The frontend calls the API at `/api`. For a deployed build, either serve the API under `/api` on the same host, or set `window.EVEREST_API = "https://api.example.com"` before `src/app.js` loads.
- CORS allows `http://localhost:5173` (the Vite dev server). To allow other addresses, set `CORS_ORIGINS` to a comma-separated list, for example `CORS_ORIGINS=http://localhost:5173,https://mill.example.com`.
- AI is optional and runs entirely on OpenRouter. Set `OPENROUTER_API_KEY` to turn it on. Jev (`typesafe/jev-1.13`) makes the decisions, and a chat model (`CHAT_MODEL`, default `google/gemini-2.5-flash`) drafts replies and reads challan photos. Without a key, every AI decision goes to the human review queue, the risk endpoint returns `null`, and the reply-draft and challan-photo endpoints return `503`.
- Settings can live in `backend/.env` (git-ignored), one `KEY=value` per line, e.g. `OPENROUTER_API_KEY=…` and `AI_THRESHOLD=0.7`. The backend loads it at start-up; real environment variables win.

## Conventions

- **Auth:** `POST /auth/login` returns a `token` (a JWT, HS256; treat it as opaque). Send it on every request as `Authorization: Bearer <token>`. Only `/auth/*` and `/track/{token}` work without it.
- **Dates:** `YYYY-MM-DD` strings. Months are `YYYY-MM` (for example `?month=2026-10`), and default to the current month.
- **Units:** money in ₹ (numbers), yarn in kg, fabric in metres.
- **Errors:** always `{"detail": "..."}`. Show `detail` to the user as is; the messages are written to be shown.

| Code | Meaning |
|---|---|
| 401 | Not logged in, bad or signed-out token, or the token has expired (after `SESSION_HOURS`, default 24). Go to login. |
| 429 | Too many failed sign-ins for that username (5 in 15 minutes). The message says how long to wait. |
| 403 | Your role can't do this. |
| 404 | Not found (for example a bad QR code). |
| 409 | A business rule blocked it, such as insufficient stock, a blocked dispatch scan, a roll not awaiting QC, or a review already resolved. Also returned for a duplicate (`Already exists: …`) or a reference to a record that doesn't exist. |
| 422 | Bad input (missing field, wrong type, value not allowed). `detail` is one readable line, for example `qty_m: Field required`. |
| 503 | The AI service is not configured or is down. |

## Roles

`owner` can do everything. The other roles can do what's listed below, and any logged-in user can read (GET) most things.

| Role | Can write |
|---|---|
| `admin` (office) | users, master data, orders, GRN, issues, jobs, machine status, production, rolls, QC, dispatch, attendance, payments, messages, reviews; can view wages, receivables and payables |
| `store` | GRN, challan photo, material issues, dispatch |
| `supervisor` | jobs, machine status, production, rolls, attendance, reviews |
| `qc` | QC results, reviews |
| `owner` only | `/reports/pnl`, `/reports/order-cost`, `/reports/machines` |

Use `GET /me` after login to get `{id, username, role}` and choose which screens to show.

## Enums

| Field | Values |
|---|---|
| user role | `owner` `admin` `store` `supervisor` `qc` |
| machine kind | `spinning` `weaving` `dyeing` |
| machine status | `Running` `Idle` `Breakdown` `Maintenance` |
| worker wage_type | `daily` `piece` (rate is per day, or per metre of output) |
| order stage | `Pending` `Spinning` `Weaving` `Dyeing` `In QC` `Ready` `Dispatched` |
| roll status | `pending_qc` `passed` `rework` `rejected` `dispatched` |
| QC result | `pass` `rework` `reject` |
| defect_type | `hole` `colour_mismatch` `uneven_weave` `stain` `other` |
| dispatch status | `open` `closed` |
| message channel | `whatsapp` `email` `phone` |
| message status | `review` (needs a human) `routed` `replied` |
| message intent | `status` `quality_complaint` `payment` `new_order` `delivery_change` `other` |
| message dept | `Office` `QC` `Production` `Dispatch` `Accounts` |
| urgency / severity | `low` `med` `high` |
| breakdown category | `mechanical` `electrical` `yarn_quality` `operator` |
| breakdown assignee | `fitter` `electrician` `store` `supervisor`. Set by rule from the category (mechanical → fitter, electrical → electrician, yarn_quality → store, operator → supervisor), not chosen by the AI. |
| stage_at_fault | `spinning` `weaving` `dyeing` |
| reply lang | `en` `ta` `hi` |

---

## Auth & users

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/auth/setup` | `{username, password, email, phone}` | Creates the first owner. Works **only once**, on an empty database. Returns the same response as login. |
| POST | `/auth/login` | `{username, password}` | Returns `{token, role, username}`. The JWT expires after `SESSION_HOURS` (default 24). Signing key: `JWT_SECRET` (32+ chars) if set, else generated once and kept in the DB. Five wrong passwords lock that username for 15 minutes (`429`). |
| POST | `/auth/logout` | none | Revokes the token (its id is kept until it would have expired) |
| GET | `/me` | none | `{id, username, role}` |
| POST | `/users` | `{username, password (min 8), role, email?, phone?}` | Admin or owner. Only an owner can create another owner. Taken username → 409. The app's form (Workforce → Add a staff account) asks for email and phone. |
| GET | `/users` | none | `[{id, username, role, email, phone}]` |

## Master data

These are generic endpoints for these tables: `buyers`, `suppliers`, `yarn_types`, `fabric_types`, `bom`, `machines`, `workers`, `shifts`.

| Method | Path | Notes |
|---|---|---|
| GET | `/master/{table}` | The list, ordered by id. Any logged-in user. |
| POST | `/master/{table}` | Create. Admin. Returns the row. |
| PUT | `/master/{table}/{id}` | Partial update; send only the fields that change. Admin. |

Fields per table (`*` = required):

| Table | Fields |
|---|---|
| buyers | `name*`, `phone`, `email`, `gstin`, `address` |
| suppliers | `name*`, `phone`, `gstin` |
| yarn_types | `count*` (for example "40s"), `material*`, `colour*`, `reorder_kg` (default 0, which means no alert) |
| fabric_types | `name*` (unique), `hsn`, `gst_rate` (default 5, as a percentage) |
| bom | `fabric_type_id*`, `yarn_type_id*`, `kg_per_m*` (kg of this yarn per metre of fabric) |
| machines | `name*` (unique), `kind*`, `overhead_per_hour` (₹), `rate_m_per_hour` (running speed; 0 = no live output). You **can't** set `status` here; use the machine status endpoint. |
| workers | `name*`, `phone`, `wage_type*`, `rate*` |
| shifts | `name*` (unique, for example "Day" or "Night"), `starts`, `ends` (free text such as "06:00") |

Unknown fields return `422`. Duplicates and bad foreign keys return `409`.

## Orders

| Method | Path | Body / query | Returns |
|---|---|---|---|
| POST | `/orders` | `{buyer_id, fabric_type_id, colour, qty_m, deadline, price_per_m}` | The order detail (below) |
| GET | `/orders` | `?buyer_id=` (optional) | A list of orders, each with tracking fields merged in |
| GET | `/orders/{id}` | none | **Order detail / enquiry view** (below) |
| GET | `/orders/{id}/feasibility` | none | A feasibility object |
| GET | `/orders/{id}/risk` | none | The deadline-risk flag |

**Order detail:**
```jsonc
{
  "id": 1, "buyer_id": 1, "buyer": "Acme", "buyer_phone": "...", "fabric_type_id": 1,
  "colour": "navy", "qty_m": 1000, "deadline": "2026-11-01", "price_per_m": 100,
  "token": "...", "created_at": "...",
  // tracking fields (same as the public tracking link):
  "order_no": 1, "fabric": "Poplin", "stage": "Weaving",
  "target_m": 1000, "produced_m": 500, "passed_m": 500,  // produced_m includes fabric still on the loom
  "dispatched_m": 0,          // shipped so far; stage is "Dispatched" only when this reaches target_m
  "rolls_produced": 2, "rolls_passed": 1, "progress_pct": 50.0,  // produced_m / target_m
  "expected_ready": "2026-10-20",   // from the last 7 days of production; null if there was none
  "dispatches": [{ "id", "vehicle_no", "driver", "status", "created_at", "rolls", "metres" }],
  // internal only:
  "tracking_path": "/track/<token>",      // share as <frontend-or-api-host>/track/<token>
  "feasibility": { ... },
  "job_cards": [{ "id", "order_id", "machine_id", "machine", "stage", "status", "created_at" }],
  "invoices": [ ... ]
}
```

**Feasibility:**
```jsonc
{
  "can_start": false, "short_kg": 120, "verdict": "short by 120 kg",  // or "can start"
  "lines": [{ "yarn_type_id", "count", "material", "colour",
              "required_kg", "issued_kg", "available_kg",
              "reserved_kg",   // still needed by earlier open orders (first come, first served by order number)
              "free_kg",       // available_kg - reserved_kg: what this order can actually count on
              "short_kg" }]
}
```

**Risk:** `{progress_pct, days_left, yarn_short_kg, free_machines, at_risk: bool | null, probability: number | null}`. `null` means the AI is unavailable, so show "unknown". This endpoint calls the AI, so call it on demand rather than in list views.

## Buyer tracking (public, no login)

`GET /track/{token}` returns the tracking fields only: `order_no, fabric, colour, stage, target_m, produced_m, passed_m, rolls_produced, rolls_passed, progress_pct, deadline, expected_ready, dispatches[]`. It returns `404` for a bad token.

## Raw material

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/grn` | `{supplier_id, yarn_type_id, qty_kg, rate, order_id?, challan_no?}` | `{grn, stock, low_stock: [...]}`. Show an alert if `low_stock` is non-empty. |
| GET | `/grn` | none | GRN list with the `supplier` name |
| POST | `/grn/challan` | `{image_b64, mime}` (`image/jpeg`, `image/png`, `image/webp` or `image/heic`; up to ~10 MB) | `{extracted: {...}, suggested: {supplier_id, yarn_type_id, qty_kg, rate, challan_no}}`. Pre-fill the GRN form from `suggested` (ids may be null), let the store keeper confirm, then `POST /grn`. |
| GET | `/stock` | none | `[{id, count, material, colour, reorder_kg, stock_kg, avg_rate, low: 0/1, ...}]`. `avg_rate` is the moving average cost of the yarn on hand (`null` when none is on hand). |
| GET | `/alerts/low-stock` | none | Yarn types at or below their reorder level |
| POST | `/issues` | `{order_id, yarn_type_id, qty_kg, machine_id?}` | `{issue, low_stock}`. The issue stores the yarn's `rate` at that moment, so later deliveries never change its cost. Returns `409 "only X kg in stock"` if there isn't enough; two people issuing the last yarn at once can't both succeed. |
| GET | `/issues` | `?order_id=` | Issue list |

## Production floor

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/jobs` | `{order_id, machine_id, operator_id?, shift_id?}` | Job card. Assigning a new job closes the machine's previous open job. |
| POST | `/jobs/{id}/done` | none | Closes the job |
| POST | `/machines/{id}/status` | `{status, note?}` | One-tap status. With `Breakdown` + `note`, the AI triages it. Returns `{log, triage}` (see the AI responses section). |
| GET | `/machines/{id}/log` | none | Status history, including triage fields |
| POST | `/production` | `{shift_id, machine_id, order_id, operator_id, output, hours?, entry_date?}` | `operator_id` is a **worker** id. `entry_date` defaults to today. `hours` drives overhead and wage costing. |
| GET | `/production` | `?entry_date=` | The latest 200 entries, with `machine`, `operator` and `shift` names |
| POST | `/rolls` | `{order_id, machine_id, shift_id, operator_id, metres}` | Returns the roll with `qr` (for example `"ETM-3F9A1C22B0"`). **Render the QR image client-side** from that string (for example with the `qrcode` npm package). |
| GET | `/rolls` | `?order_id=&status=` | Roll list for pick lists, e.g. an order's passed rolls waiting for a truck (max 500) |
| GET | `/rolls/{qr}` | none | **Traceability view** (below) |
| GET | `/floor` | none | Live floor dashboard (below) |

**Floor dashboard:**
```jsonc
{
  "machines": [{ "id", "name", "kind", "status", "overhead_per_hour",
                 "order_id",            // current open job, or null
                 "order_progress_pct",  // null if no job
                 "today_output",
                 "idle": true }],       // true when status != Running; highlight these
  "idle_count": 2,
  "orders": [ /* tracking objects for orders currently on machines */ ]
}
```

**Traceability (`/rolls/{qr}`):**
```jsonc
{
  "id", "qr", "metres", "status", "made_on",
  "order_id", "buyer", "machine": "Loom 4", "stage": "weaving",
  "shift": "Night", "operator": "Operator X",
  "order": { /* tracking object */ },
  "yarn_issued": [{ "qty_kg", "count", "material", "colour", "issued_on", ... }],
  "qc_history": [{ "result", "defect_type", "severity", "stage_at_fault", "notes", "inspector", "at" }],
  "dispatch": { "id", "vehicle_no", "driver", "status" } // or null
}
```

### Live floor

A machine that is **Running** with an open job card and a speed (`rate_m_per_hour` > 0) produces output on its own:

- Every request brings running machines up to date (at most every `ACCRUE_EVERY` seconds, default 2): metres = speed × time since the last update, written to one auto production entry per machine, order, operator, shift and day (`auto: 1`). Progress, wages and machine-hour costs follow.
- Looms use yarn by the fabric recipe: first the yarn already issued to the order ("yarn at loom"), then store stock, issued automatically at the moving-average rate (issues with no `created_by`).
- Out of yarn → the machine goes **Idle** and the log says `stopped: out of …`. Stage complete → the job card closes, the machine goes **Idle**, the log says `finished: …`.
- Output is credited to the job card's operator and shift, else to whoever last ran the machine. Setting **Running** with neither returns `422`.
- `SIM_SPEED` fast-forwards machine time for demos (60 = one machine-hour per real minute). Default 1. Spinning is not run live (its output is kg).
- `/floor` machines add: `job_id`, `operator`, `shift`, `running_since`, `stage_done_m`, `qty_m`, `finishes_at` (UTC), `yarn_kg_per_hour`, `yarn_at_loom: [{yarn, kg, store_kg}]`, `last_note`; the response adds `sim_speed`.

## Quality control

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/qc/queue` | none | Rolls with status `pending_qc` or `rework`, plus the `machine` name |
| POST | `/qc/{qr}` | `{result, defect_type?, notes?}` | `rework` and `reject` **require** `defect_type` or `notes`. Returns `{qc, classification}`. Returns `409` if the roll isn't awaiting QC. |

The rework loop: a roll marked `rework` goes back into the QC queue, and the next scan records its new result. If you send `notes` on a rework or reject, the AI classifies the defect type, severity and stage at fault. If you also send `defect_type`, your value is kept and the AI fills in the rest.

## Finished goods & dispatch

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/finished-goods` | none | Passed rolls grouped by order: `{order_id, buyer, fabric, colour, rolls, metres}` |
| POST | `/dispatches` | `{order_id, vehicle_no, driver}` | Opens a truck |
| POST | `/dispatches/{id}/scan` | `{qr}` | Adds a roll. Returns the dispatch detail. **Guards return `409`** with a `detail` beginning `BLOCKED:` when the roll is already scanned, belongs to another order, or isn't QC-passed. |
| POST | `/dispatches/{id}/close` | none | Closes the truck and **generates the invoice**. Returns the dispatch detail with `invoice`. |
| GET | `/dispatches` | none | History: `{..., buyer, invoice_id, total}` |
| GET | `/dispatches/{id}` | none | Dispatch detail (below). Use it to render the invoice and packing list. |

**Dispatch detail:**
```jsonc
{
  "id", "order_id", "vehicle_no", "driver", "status", "created_at",
  "buyer", "buyer_gstin", "buyer_address", "fabric", "colour",
  "packing_list": [{ "qr", "metres" }], "rolls": 2, "metres": 1000,
  "invoice": { "id", "metres", "rate", "subtotal", "gst_rate", "gst", "total", "issued_on" } // null until closed
}
```
Format the invoice number on the frontend, for example `INV-` followed by the zero-padded invoice id.

## Workers & wages

| Method | Path | Body / query | Notes |
|---|---|---|---|
| POST | `/attendance` | `{shift_id, worker_ids: [..], work_date?}` | Marks a whole shift in one go. Marking a worker twice is harmless. Returns that shift's attendance. |
| GET | `/wages` | `?month=` | `{month, total, workers: [{id, name, wage_type, rate, days, output, wage}]}` |

## Accounts & owner dashboard

| Method | Path | Body / query | Returns |
|---|---|---|---|
| POST | `/payments` | `{party: "buyer"/"supplier", party_id, amount, invoice_id?, paid_on?, ref?}` | The payment. `party_id` must be an existing buyer or supplier (`422` otherwise). An `invoice_id` must belong to that buyer. |
| GET | `/reports/receivables` | none | One row per invoice: `{..., buyer, paid, outstanding}` |
| GET | `/reports/payables` | none | One row per supplier: `{id, name, billed, paid, outstanding}` (bills are the GRNs) |
| GET | `/reports/order-cost` | none | All orders: `[{order_id, yarn, wages, overhead, total_cost, revenue, margin}]` |
| GET | `/reports/order-cost/{id}` | none | The same for one order |
| GET | `/reports/machines` | `?month=` | `[{id, name, kind, output, hours, breakdowns}]` |
| GET | `/reports/pnl` | `?month=` | `{month, revenue, yarn, wages, overhead, total_costs, profit}` (revenue excludes GST) |

## Buyer messages & AI

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/messages` | `{text, channel?, buyer_id?, order_id?}` | Logs an incoming message and has the AI route it. Returns `{message, routing}`. |
| GET | `/messages` | `?status=&dept=` | The inbox. Filter by `dept` for each department's view. |
| POST | `/messages/{id}/draft` | `{lang}` | `{draft}`, a reply drafted from live order data. Also saved on the message. |
| POST | `/messages/{id}/reply` | `{text}` | Marks the message `replied` with the final text. **It does not send anything**; actual WhatsApp or email delivery isn't built. |
| GET | `/reviews` | `?status=open\|resolved\|all&kind=` | Every AI decision. `open` (the default) is the human review queue; `resolved` includes decisions applied automatically, which can still be corrected |
| GET | `/reviews/accuracy` | none | Admin/owner. For each kind: decisions, how many were auto-applied, how many a person checked, and how often Jev's main decision matched the person's, per confidence band. Use it to set `AI_THRESHOLD` |
| POST | `/reviews/{id}` | none, or `{field: value, ...}` | Open item: **no body accepts the AI's suggestion**; fields you send correct it. Settled item (auto-applied or already decided): send fields to correct it; an empty body returns `409`. Values must come from the enums. Returns `{ok, applied}`. A resolved message becomes `routed` (a replied one stays `replied`). |

**AI responses** (`routing`, `triage` and `classification` all have this shape):
```jsonc
{
  "auto": true,               // false means it was sent to the review queue
  "review_id": 12,            // every decision is logged; POST /reviews/12 corrects it
  "confidence": 0.94,         // show this, for example "Production (0.94)"
  "suggestion": { "intent": "status", "dept": "Production", "urgency": "med" },
  "probs": { "intent": { "status": 0.94, ... }, ... }
}
```
`confidence` is for the main decision: `dept` for messages, `category` for breakdowns, and `defect_type` (or `stage_at_fault` if the inspector chose the defect) for QC. The other fields are hints. A message whose intent comes back `other` always goes to review.

When `auto` is `false`, the fields on the message, log or QC record stay **empty** until someone resolves them in `/reviews`.

**Review queue item:**
```jsonc
{ "id", "kind": "message" | "breakdown" | "defect", "ref_id", // id of the message / machine log / QC record
  "suggestion": { ... },      // what Jev chose
  "final": { ... } | null,    // what a person confirmed or corrected; null = nobody has checked it
  "auto": false, "confidence": 0.41, "status": "open" | "resolved", "created_at" }
```

## Typical screen → endpoint map

| Screen | Endpoints |
|---|---|
| Login | `/auth/login`, `/me` |
| Store: GRN | `/grn/challan` → `/grn`; `/stock`; `/alerts/low-stock` |
| Store: issue yarn | `/orders`, `/stock` → `/issues` |
| Office: new order | `/master/buyers`, `/master/fabric_types` → `/orders` (show `feasibility.verdict`) |
| Office: enquiry | `/orders?buyer_id=` → `/orders/{id}` |
| Supervisor: floor | `/floor`, `/machines/{id}/status`, `/jobs`, `/production`, `/rolls` |
| QC: scan | camera → `/rolls/{qr}` → `/qc/{qr}`; `/qc/queue` |
| Dispatch | `/finished-goods` → `/dispatches` → scan loop `/dispatches/{id}/scan` → `/dispatches/{id}/close` |
| Inbox | `/messages` → `/messages/{id}/draft` → `/messages/{id}/reply`; `/reviews` |
| Owner dashboard | `/reports/pnl`, `/reports/order-cost`, `/reports/machines`, `/wages`, `/reports/receivables`, `/reports/payables` |
| Buyer page | `/track/{token}` (no login) |

## Alerts, PO drafts and the owner digest
See [automation.md](automation.md): `/alerts/defects`, `/reports/defects`, `/purchase-orders`, `/po-lines/{id}`, `/digest/today`. `POST /grn` also takes an optional `po_line_id`; `POST /orders`, `/grn` and `/issues` return `po_refresh`; `POST /qc/{qr}` returns `defect_alerts`.

## Code layout (backend/)

`app.py` only assembles the app. Each business area is one module in `mill/` holding its request models, rules and
`router`; add an endpoint to the module for its area, and a new area as a new module listed in `app.py`.

| Module | What it owns |
|---|---|
| `config` | Settings from the environment and `backend/.env` |
| `db` | Connections, schema setup (`db/*.sql`), `tx()` write lock, `rows/one/insert` |
| `auth` | Passwords, JWTs, `user` / `role(...)` dependencies, `/auth/*`, `/users` |
| `ai` | Every OpenRouter call (Jev and the chat model) |
| `masters`, `orders`, `inventory`, `production`, `quality`, `dispatch`, `workforce`, `accounts`, `messages` | Their endpoints and rules |
| `reviews` | Jev decisions with the confidence gate (`decide`) and the review queue |
| `livefloor` | Running machines produce output and use yarn (called before each request) |
| `defects`, `purchasing`, `digest` | Alerts, PO drafts and the owner digest (see automation.md) |

Shared rules are imported from the module that owns them (e.g. `from mill.orders import tracking`), never copied.

## Attendance register
`GET /attendance?month=YYYY-MM` (admin, supervisor; default this month) → `{month, records: [{worker_id, work_date, shift_id, shift}]}`, one record per worker per shift present.
