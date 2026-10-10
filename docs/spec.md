# WeaveSphere — what has been built

WeaveSphere is the operating system for Everest Textile Mills: one web app that follows yarn in, fabric out, and money in,
replacing the notebooks and phone calls a mill runs on. Every role in the mill gets its own screens; buyers get one link.
AI (Jev for decisions, a chat model for words) helps where judgement or language is needed, and a person can always check it.

This document describes the system as it stands. The phone/voice order desk is built but left out of this document's demo scope.

---

## 1. Running it

| What | Command (from the repo root) | Address |
|---|---|---|
| Backend API | `cd backend && python3 -m uvicorn app:app --port 8000` | http://localhost:8000 (API docs at `/docs`) |
| Frontend | `cd frontend && npm install && npx vite --port 5173` | http://localhost:5173 |
| Fresh demo data | `cd backend && DB_PATH=demo.db python3 seed.py` (then start the backend with `DB_PATH=demo.db`) | |
| Tests | `cd backend && python3 -m pytest -q tests` | |

The frontend talks to the backend through `/api` (Vite proxies it to port 8000). AI needs `OPENROUTER_API_KEY` in
`backend/.env`; without it everything still works and AI decisions go to the human review queue.

**Demo accounts** (created by `seed.py`; password for all: `everest-demo`)

| Username | Role | Lands on |
|---|---|---|
| `owner` | Owner | Everything, including Reports and the morning digest |
| `admin` | Office / Admin | Orders, inbox, dispatch paperwork, accounts, masters |
| `store` | Store Keeper | Yarn receipts and issues, truck loading |
| `floor` | Floor Supervisor | Machines, job cards, shift output, rolls, attendance |
| `qc` | QC Inspector | Roll inspection, defects, traceability |

Buyers have no account: each order has a secret tracking link (`/track.html#track=…`).

---

## 2. Who sees what

| Page | Owner | Office | Store | Floor | QC |
|---|:-:|:-:|:-:|:-:|:-:|
| Dashboard (role-specific) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Orders | ✓ | ✓ | ✓ | ✓ | |
| Production | ✓ | ✓ | | ✓ | |
| Inventory (yarn, POs) | ✓ | ✓ | ✓ | | |
| Quality | ✓ | ✓ | | | ✓ |
| Traceability | ✓ | ✓ | ✓ | ✓ | ✓ |
| Dispatch | ✓ | ✓ | ✓ | | |
| Tracking (office view of buyer status) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Inbox (buyer messages) | ✓ | ✓ | | | |
| Reviews (AI decisions to check) | ✓ | ✓ | | ✓ | ✓ |
| Workforce | ✓ | ✓ | | ✓ | |
| Accounts | ✓ | ✓ | | | |
| Masters | ✓ | ✓ | | | |
| Reports | ✓ | | | | |

The server enforces the same rules on every endpoint (the owner may do anything). Pages hide what a role can't do.

---

## 3. Features

### 3.1 Sign-in and security
- Usernames and passwords; passwords hashed with scrypt. First-time setup creates the owner on an empty database only.
- Sign-in returns a signed **JWT (HS256)** valid for 24 h (`SESSION_HOURS`). Signing key: `JWT_SECRET` if set
  (32+ characters, refused otherwise), else one generated once and kept in the database.
- Sign-out revokes that token immediately. The user's role is re-read on every request, so a role change applies at once.
- **Lockout:** 5 wrong passwords in 15 minutes locks that username for 15 minutes. Unknown usernames take as long to
  reject as wrong passwords, so the timing doesn't reveal which accounts exist.
- Every value shown in the browser is HTML-escaped; CSV exports guard against spreadsheet formula injection.

### 3.2 Masters (setup data)
Buyers, suppliers, yarn types, fabric types, fabric recipes (bill of materials: kg of each yarn per metre),
machines (kind, overhead ₹/hour, running speed m/hour), workers (daily or piece rate), shifts. Office/Owner add and edit;
everyone can read. Machine status can only change through the status control, so every change is logged.

### 3.3 Orders
- An order: buyer, fabric, colour, metres, deadline, price per metre. Creating one runs a yarn check immediately.
- **Stage** is worked out, never typed: Pending → Spinning / Weaving / Dyeing (from job cards) → In QC (rolls waiting)
  → Ready (passed metres ≥ ordered) → Dispatched (shipped metres ≥ ordered; part-shipped orders stay open).
- **Yarn feasibility:** the recipe × metres against stock, after reserving stock for earlier open orders (first come,
  first served). Shows each yarn's need, issued, in stock and shortfall.
- **Deadline risk:** Jev estimates the chance of missing the deadline from progress, days left, yarn shortfall and idle
  machines. Shows "unknown" if the AI is unavailable.
- **Expected ready date:** from the last 7 days' output rate on the furthest stage reached.
- Each order has a secret tracking link for its buyer (see 3.11).

### 3.4 Yarn store
- **Goods receipt (GRN):** supplier, yarn, kg, rate, challan number, optional order and optional purchase-order line.
- **Challan photo:** the chat model reads a photo of the delivery challan and pre-fills the GRN form (matched to
  known suppliers and yarns); the store keeper checks and saves.
- **Live stock** is a database view: receipts minus issues, valued at the moving-average rate. Nothing to reconcile.
- **Issue yarn** to an order (and optionally a machine), at the average rate at that moment. The last kg can't be
  issued twice even if two people press at once (write lock).
- **Low-stock alerts** against each yarn's reorder level.

### 3.5 Purchasing (purchase-order drafts)
- For each yarn: **need = what open orders still need + reorder level − stock − yarn already on order.**
  When need > 0 a draft PO line is made, from the supplier whose latest rate is lowest (last 10 receipts), with the
  reasoning written on the line.
- Drafts are recomputed after every order, receipt and issue. A person's work is never overwritten: approved POs,
  POs raised by hand and lines someone edited count as "on order" and are left alone.
- Office/Owner approve ("placed with the supplier"), edit quantities, or cancel (not once yarn has arrived).
- Deliveries are booked against an approved PO line from the GRN form; a PO shows "received" once its lines are met.

### 3.6 Production and the live floor
- **Job cards** put an order on a machine with an operator and shift (one open job per machine).
- **One-tap machine status:** Running, Idle, Breakdown, Maintenance — every change logged.
- **Live floor:** a running loom or dye line produces output at its speed while it runs. On looms, yarn is consumed
  as metres are woven: first yarn already issued to the order, then store stock (issued automatically at the average
  rate). The machine stops itself, with a note, when it runs out of yarn or the order's stage is complete.
  Each machine card shows metres done / ordered, speed, yarn use per hour, yarn at the loom and the estimated finish.
- **Breakdowns:** the note goes to Jev, which sorts it (mechanical, electrical, yarn quality, operator) and severity;
  who to send follows from the category by rule (fitter, electrician, store, supervisor).
- **Shift production entries** by hand, optionally creating a roll in the same step.
- **Rolls** get a unique code (`ETM-…`) and a QR label (see 3.8).

### 3.7 Quality
- QC queue of rolls waiting or back from rework. One tap to pass; rework or reject needs a defect type or notes.
- From the inspector's notes, Jev classifies the defect type, the stage at fault and severity.
- Rejected rolls can never be dispatched; reworked rolls return to the queue.
- **Defect hotspots:** after every QC result, the roll's machine + shift and its operator are re-checked over 30 days.
  An alert opens when there are ≥ 5 checks and the failure rate is ≥ 2× the rest of the mill and ≥ 15 points above it.
  The alert carries a maintenance note written by code ("Have a fitter check warp tension…"), updates as checks
  come in, can be acknowledged ("we're on it"), and clears itself when the rate is normal again.
- **Defect breakdown:** failures grouped by machine + shift, machine, shift, operator or defect type.

### 3.8 Traceability and the QR system
- **Trace any roll:** order, buyer, fabric, metres, machine, shift, operator, made date, yarn issued to the order,
  full QC history, and the truck it left on.
- **Roll labels:** a printable 100 × 75 mm sticker — QR, roll code, metres, order and buyer, fabric and colour,
  loom, shift, operator, date. Printing gives exactly one label-sized page. The QR generator is bundled (works offline).
- The QR holds the roll's link, so **any phone camera** opens its record (after sign-in).
- **Scanning in the app:** every roll-code box (QC, Traceability, each loading truck) has a **SCAN** button that opens
  the device camera. Trace opens the roll; QC fills the roll and moves to the result; truck loading keeps scanning
  roll after roll. Uses the browser's own QR detector, or jsQR on iPhones.
- **Scanner guns** work too: the label link they type is understood everywhere (server and page).

### 3.9 Dispatch and invoicing
- Open a truck (order, vehicle, driver), then scan or tap passed rolls onto it.
- Hard rules, no AI: the truck must be open; a roll can be on only one truck; it must belong to that order; it must
  have passed QC.
- **Closing the truck** marks its rolls dispatched and raises the **GST invoice** (metres × order price, GST at the
  fabric's rate). Printable invoice and packing list.
- Partial dispatch is normal: an order is "Dispatched" only when all its metres have shipped.

### 3.10 Accounts and reports
- **Payments** from buyers (always against an invoice, never more than is due) and to suppliers.
- **Receivables** per invoice (total, paid, outstanding) and **payables** per supplier (receipts − payments).
- **Cost per order:** yarn issued (at the rates used) + labour (piece output × rate, or day rate split by hours) +
  machine overhead (hours × ₹/hour), against invoiced revenue → margin.
- **Monthly P&L** (owner): revenue ex-GST − yarn − wages − overhead.
- **Machine report:** output, hours and breakdowns per machine per month.
- All tables export to CSV.

### 3.11 The buyer's page (tracking link)
- A standalone page, outside the staff app: the order's fabric and colour, stage timeline, progress bar, metres passed
  and dispatched, trucks, expected ready date and deadline. Refreshes every minute.
- The link is the only key: it shows that one order and nothing else (no prices, no other orders).
- Office copies the link from the order (Copy share link) and can preview it (Open buyer page). Old links redirect.
- Works on phones (stacked layout).

### 3.12 Buyer inbox
- Office logs a buyer message (WhatsApp, email or phone), optionally with buyer and order (an order fills in its buyer;
  a mismatched pair is refused).
- **Jev routes** it: department (Office, QC, Production, Dispatch, Accounts), intent and urgency. Unsure or unclear
  messages go to the review queue instead.
- **Reply drafts** in English, Tamil or Hindi, written by the chat model from that buyer's live orders and the mill's
  fabric catalogue (prices and dates are never invented). Office edits, copies it into WhatsApp/email, and marks it sent.
  An unsent draft is labelled "Draft, not sent".

### 3.13 AI review queue (human in the loop)
- Every AI decision is logged. Below the confidence threshold (`AI_THRESHOLD`, 0.8; demo 0.7) it waits for a person;
  above it, it is applied and can still be corrected.
- Each kind belongs to its team: buyer messages → Office; breakdowns → Floor; defects → QC.
- Only answers Jev actually gave are applied; anything it couldn't answer is left for a person.
- **Accuracy report:** how often the AI and people agreed, by confidence band — the evidence for setting the threshold.

### 3.14 Owner's morning digest
- One panel on the Owner and Office dashboards: yesterday's output, low stock, PO drafts waiting, orders at risk,
  machines not running, overdue invoices (30 days, `CREDIT_DAYS`), defect alerts, reviews waiting.
- Facts are counted by code. The chat model only words the summary; if it writes any number not in the facts, its text
  is discarded and a plain template is used. Built on the first visit each day, or at a set hour (`DIGEST_HOUR`).

### 3.15 Workforce
- Attendance per shift (tick everyone present).
- **Attendance register:** pick any month to see who was present, day by day — workers down the side, days across,
  each mark a shift present (D, N…), Sundays shaded, today outlined, and a DAYS total that matches the wages.
  Owner, Office and Floor Supervisor; scrolls sideways on a phone with names pinned; exports to CSV.
- Monthly wages (Owner, Office): daily rate × days present, or piece rate × output
  (live floor output is credited to the job card's operator).

### 3.16 Everywhere
- Live screens refresh every 10 s, but never while someone is typing, has a form half-filled or a dialog open.
- Navigation in English or Tamil. Errors shown as plain-language messages; a result notice (e.g. Jev's triage)
  stays on screen through the auto-refresh until your next action.
- Works on phones: tables become cards, inputs don't trigger zoom, and the camera scanner is built in.
- Business dates are the mill's local date (India), not UTC, so the night shift's entries land on the right day.

---

## 4. Where AI is used

| Use | Model | If unavailable |
|---|---|---|
| Buyer message routing | Jev (`typesafe/jev-1.13`) | Goes to the review queue |
| Breakdown triage | Jev | Goes to the review queue |
| Defect classification | Jev | Goes to the review queue |
| Deadline risk | Jev | Shows "unknown" |
| Orders at risk in the digest | Jev (confirms rule-flagged orders, at most 8) | The rule's verdict stands |
| Reply drafts (EN/TA/HI) | Chat model (`google/gemini-2.5-flash`) | "Drafter unavailable" message |
| Challan photo reading | Chat model (vision) | Type the GRN by hand |
| Digest wording | Chat model | Plain template |

All through OpenRouter with one key. AI never does arithmetic: stock, costs, invoices, PO quantities and alerts are code.

---

## 5. Architecture

- **Backend:** Python 3.12, FastAPI, SQLite (WAL mode, STRICT tables, parameterised SQL, `BEGIN IMMEDIATE` write locks).
  `backend/app.py` assembles one router per business area in `backend/mill/`:
  `config, db, auth, ai, masters, orders, inventory, purchasing, production, livefloor, quality, defects, dispatch,
  workforce, accounts, messages, reviews, digest`. About 70 endpoints.
- **Frontend:** plain JavaScript modules built with Vite: `index.html` + `src/app.js` (landing page and staff app, GSAP
  scroll animation) and `track.html` + `src/track.js` (the buyer's page). Libraries: GSAP, qrcode, jsQR.
- **Data (32 tables and one view):** users, app_secrets, revoked_tokens · buyers, suppliers, yarn_types, fabric_types,
  bom, machines, workers, shifts · orders · grn, material_issues, `yarn_stock` (view) · purchase_orders, po_lines,
  po_receipts · job_cards, machine_status_log, production_entries, attendance · rolls, qc_records, defect_alerts ·
  dispatches, dispatch_items, invoices, payments · buyer_messages, ai_reviews, digests.
- **Derived, never stored:** stock, order stage, PO "received", outstanding amounts.

---

## 6. Settings (`backend/.env` or environment)

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Enables the AI features |
| `AI_THRESHOLD` | 0.8 | Confidence needed to apply an AI decision without a person |
| `JEV_MODEL` / `CHAT_MODEL` | `typesafe/jev-1.13` / `google/gemini-2.5-flash` | Models used |
| `DB_PATH` | `backend/mill.db` | Database file |
| `SESSION_HOURS` | 24 | Sign-in lifetime |
| `JWT_SECRET` | generated once | Token signing key (32+ chars if set) |
| `SIM_SPEED` | 1 | Live-floor time multiplier for demos (e.g. 60 = an hour per minute) |
| `ACCRUE_EVERY` | 2 | Seconds between live-floor updates |
| `DEFECT_DAYS`, `DEFECT_MIN_CHECKS`, `DEFECT_MULT`, `DEFECT_POINTS` | 30, 5, 2, 0.15 | Hotspot alert rules |
| `CREDIT_DAYS` | 30 | Days until an invoice is overdue |
| `DIGEST_HOUR` | unset | Build the digest at this hour daily (else on first visit) |
| `CORS_ORIGINS` | `http://localhost:5173` | Allowed browser origins |

---

## 7. Demo data (`seed.py`)

Two buyers (Meridian Apparel, North Loom Studio), two suppliers, three yarns, three fabrics with recipes, four machines
(Spinning 01 in maintenance, Loom 01, Loom 04 running, Dye line 01), three workers, Day and Night shifts.
Three orders: #1 Meridian organic poplin 1,200 m (in production, yarn issued, 600 m logged, three 200 m rolls waiting
for QC), #2 North Loom recycled twill 860 m (short of polyester → a PO draft), #3 Meridian canvas 1,050 m.

---

## 8. Demo walkthrough (about 10 minutes)

1. **Landing page** → scroll the story → Sign in as `owner`.
2. **Dashboard:** the morning digest, KPIs, live machine board (Loom 04 weaving order #1).
3. **Orders:** order #2 → YARN (short of polyester) → RISK (Jev's estimate).
4. **Inventory:** the PO draft for ReThread Fibres with its reasoning → APPROVE → receive it in the GRN form
   ("Against PO"); try the challan photo (`docs/demo/sample-challan.jpg`).
5. **Production:** machine cards with live progress; set a machine to Breakdown with a note → Jev's triage.
6. **Quality:** PASS a roll; reject one with notes → Jev classifies it; Defect hotspots and the breakdown chart.
7. **Traceability:** trace a roll → QR LABEL → PRINT (one 100 × 75 mm label); SCAN with the camera.
8. **Dispatch:** open a truck for order #1 → load passed rolls (tap or SCAN) → CLOSE TRUCK & INVOICE → print.
9. **Accounts:** record the buyer's payment against the invoice; receivables update.
10. **Inbox:** log "When will order 2 be ready?" for North Loom → Jev routes it → DRAFT REPLY in Tamil.
11. **Reviews:** correct an AI decision; see the accuracy report.
12. **Tracking → order #2 → Open buyer page:** the buyer's view on a phone-sized window.
13. **Reports** (owner): P&L, cost per order, machine output.

---

## 9. Known limits (by design, for now)
- Buyer payments must name an invoice (advances aren't tracked yet).
- A defect alert clears only when a new QC check comes in for that machine/shift or operator.
- No way to unload a wrongly scanned roll from an open truck; marking a job done leaves the machine "Running".
- Deliveries may exceed a PO line; one mill, one currency (INR), no e-way bills.
- Phone cameras need the app on HTTPS (or localhost); scanner guns and typing work anywhere.
- Lockout and rate limits are per server process.
