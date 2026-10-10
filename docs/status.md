# WeaveSphere: Backend Status

*Everest Textile Mills MVP · as of 2026-10-10*

The **backend is complete** for every MVP feature in the spec (§3.0–§4). It is tested end to end against the demo script, and the AI side has been checked live on OpenRouter with a real key. The **frontend is wired to the backend**: every screen reads and writes through the API, sign-in is checked by the server, and buyers track orders by link without an account. [backend.md](backend.md) is the API guide.

---

## 1. At a glance

| | |
|---|---|
| Stack | Python 3.12 · FastAPI · SQLite (WAL mode). No ORM, no other dependencies. |
| AI | OpenRouter only. **Jev** (`typesafe/jev-1.13`) makes decisions; a chat model (`google/gemini-2.5-flash`) writes text and reads challan photos. |
| Size | ~890 lines of API, 230 of schema, 76 for the AI calls |
| Endpoints | 54 |
| Tests | 1 end-to-end test of the whole demo flow (passing) and 1 live AI check (passing) |
| Git | `main`, restructured into `backend/`, `frontend/` and `docs/`. **The OpenRouter switch, the gate fixes, the improvements ported from the other backend, and `smoke_ai.py` are not committed yet.** |

### Files

| File | What it is |
|---|---|
| [backend/app.py](../backend/app.py) | All endpoints and business rules |
| [backend/db/schema.sql](../backend/db/schema.sql) | 25 tables + 1 view. Strict typing; CHECK, UNIQUE and foreign-key rules enforce the hard rules in the database itself. |
| [backend/ai.py](../backend/ai.py) | The OpenRouter calls: `decide`, `probability`, `draft_reply`, `read_challan` |
| [backend/tests/test_app.py](../backend/tests/test_app.py) | Offline test that runs demo steps 1–8 with a fake Jev |
| [backend/smoke_ai.py](../backend/smoke_ai.py) | Live check against OpenRouter (costs a few cents) |
| [docs/backend.md](backend.md) | API guide for the frontend team |
| backend/requirements.txt / .gitignore | The usual |

### Run

```bash
cd backend && pip install -r requirements.txt
```

```bash
AI_THRESHOLD=0.7 OPENROUTER_API_KEY=sk-or-v1-... uvicorn app:app --reload
```

The API docs are at `http://localhost:8000/docs`. On a fresh database, call `POST /auth/setup` once to create the owner account. CORS allows `http://localhost:5173` (change it with `CORS_ORIGINS`).

---

## 2. Spec coverage

✅ built · ⚠️ built with a known limit · ❌ not built

### 3.0 Platform
| Feature | | Notes |
|---|---|---|
| Role-based login | ✅ | owner / admin / store / supervisor / qc. Bearer-token sessions; passwords hashed with scrypt. The owner can do everything. |
| Master data | ✅ | One set of create/read/update endpoints for buyers, suppliers, yarn types, fabric types, BOM, machines, workers and shifts. Field names are checked against the database schema. |
| BOM (kg yarn per metre) | ✅ | One row per fabric type × yarn type |
| QR generation + scan | ⚠️ | The backend issues a unique code per roll (`ETM-XXXXXXXXXX`) and looks rolls up by it. The QR **image** and the **camera scan** are frontend work. |

### 3.1 Raw material
| Feature | | Notes |
|---|---|---|
| GRN entry | ✅ | supplier, yarn type, qty, rate, optional order and challan number |
| Live yarn stock | ✅ | Calculated live from GRNs minus issues, never stored, so it can't drift |
| Reorder level + low-stock alert | ✅ | Every GRN and issue response includes the current low-stock list, and `/alerts/low-stock` returns it on its own |
| Material issue | ✅ | Locked check: you can't issue more yarn than is in stock (`409`) |
| Order feasibility | ⚠️ | Returns "can start" or "short by X kg" for each yarn type. It checks one order at a time, so two orders can both count on the same stock. |

### 3.2 Orders
| Feature | | Notes |
|---|---|---|
| Create order | ✅ | |
| Yarn requirement from BOM | ✅ | Returned with every order |
| Derived status | ✅ | `Pending → Spinning → Weaving → Dyeing → In QC → Ready → Dispatched`, worked out from job cards, rolls and dispatches |
| Deadline-risk flag | ✅ | Jev yes/no question. The code works out the inputs (progress, days left, yarn shortfall, free machines) and Jev returns a probability. |

### 3.3 Production floor
| Feature | | Notes |
|---|---|---|
| Job card (order → machine) | ✅ | One open job per machine; assigning a new job closes the old one |
| One-tap machine status | ✅ | Every change is logged. A breakdown with a note goes to Jev for triage. |
| Shift production entry | ✅ | Includes `hours`, which feeds overhead and wage costing |
| Live floor dashboard | ✅ | Order on each machine, % complete, idle machines flagged, today's output |
| Roll creation + QR code | ✅ | Linked to order, machine, shift and operator |

### 3.4 Quality control
| Feature | | Notes |
|---|---|---|
| Pass / Rework / Reject | ✅ | Rework and reject need a defect type or notes |
| Defect type + notes | ✅ | When the inspector writes notes, Jev fills in the defect type, stage at fault and severity |
| QC queue | ✅ | |
| Rework loop | ✅ | A rework roll goes back into the queue; a roll that's already been decided is blocked |
| Traceability view | ✅ | Roll → order, buyer, machine, shift, operator, yarn issued, QC history, dispatch |

### 3.5 Buyer tracking
| Feature | | Notes |
|---|---|---|
| Token tracking link (no login) | ✅ | `/track/{token}` |
| Stage, rolls vs target, ETA, dispatch | ⚠️ | The ETA is based on the last 7 days of output, and is `null` if there was none |
| Internal enquiry view | ✅ | `/orders/{id}` |

### 3.6 Finished goods & dispatch
| Feature | | Notes |
|---|---|---|
| Finished-goods stock | ✅ | Passed rolls grouped by order, fabric and colour |
| Dispatch + scan rolls | ✅ | |
| Guards | ✅ | Blocks a roll that's already on a dispatch, belongs to another order, or isn't QC-passed. The database also stops a roll being on two dispatches. |
| Invoice + packing list | ✅ | Created when the truck is closed. GST rate comes from the fabric type. |
| Dispatch history | ✅ | |

### 3.7 Workers & wages
| Feature | | Notes |
|---|---|---|
| Shift attendance | ✅ | A whole shift in one call |
| Wage rule (daily / piece) | ✅ | |
| Monthly wages | ⚠️ | Daily workers are paid per day present. A double shift isn't paid extra, because overtime is out of scope. |

### 3.8 Accounts & owner dashboard
| Feature | | Notes |
|---|---|---|
| Cost per order | ⚠️ | Yarn × average GRN rate, plus wages (piece workers by output; daily workers by their share of hours that day), plus machine-hours × overhead. Yarn is priced at the **average** rate, not first-in-first-out. |
| Output per machine | ✅ | Output, hours and breakdowns for the month |
| Total wages | ✅ | |
| Supplier bills + payments | ✅ | GRNs are the bills; payables report |
| Buyer payments + receivables | ✅ | A payment linked to an invoice must come from that invoice's buyer |
| Monthly P&L | ✅ | Revenue (excluding GST) minus yarn, wages and overhead |

### 4. AI layer
| Feature | | Notes |
|---|---|---|
| Buyer message routing ⭐ | ✅ | Jev answers department, intent and urgency in a single call |
| Breakdown triage | ✅ | Jev picks the category and severity; who to send follows from the category by rule |
| QC defect classification | ✅ | Defect type, stage at fault, severity |
| Order risk flag | ✅ | Jev yes/no question |
| Confidence gate + human review queue | ✅ | `/reviews` lists items; a person accepts or corrects them, and only the allowed values are accepted |
| Buyer reply draft (English / Tamil / Hindi) | ✅ | Verified live in Tamil; it uses only the live order data |
| Challan photo → GRN | ⚠️ | Built, but **not yet tested with a real photo**. The store keeper confirms the suggestion before the GRN is saved. |
| Never AI for numbers | ✅ | Stock, guards, invoices, wages, costs and P&L are all plain code |
| Fallback | ✅ | If Jev is down, every decision goes to human review and nothing breaks. (The spec's generic `classify()` was replaced by Jev's own multi-question call.) |
| Actually sending replies | ❌ | `/messages/{id}/reply` records the reply but sends nothing. There is no WhatsApp or email connection. |

---

## 3. How the AI decides

```
event ──► code builds the state (live order data, machine info, notes)
      ──► Jev: one call, several questions ──► {choice, probabilities, confidence} for each
      ──► gate: main decision's confidence ≥ AI_THRESHOLD  AND  message intent ≠ "other"
             ├─ yes ► write the fields to the message / machine log / QC record (message → "routed")
             └─ no  ► leave the fields empty, add an item to /reviews, a human decides
```

- **Main decision:** `dept` for messages, `category` for breakdowns, `defect_type` for QC (or `stage_at_fault` if the inspector already picked the defect type). Other fields are hints.
- Jev's `confidence` is **not** the top probability. Jev describes it as how concentrated the probabilities are, and it is usually lower than the top probability.

### Live results (OpenRouter, two runs)

| Case | Main decision confidence | At 0.8 | At 0.7 |
|---|---|---|---|
| "When will order 12 be ready?" → Production | 0.85 / 0.80 | only just passes | ✅ routed automatically |
| "hmm ok" → intent `other` | n/a | ✅ review | ✅ review |
| Loom 4 "yarn keeps snapping" → yarn quality | 0.77 / 0.73 | ❌ goes to review | ✅ applied automatically |
| Order risk (30% done, 4 days left, short 120 kg) | P(late) 0.87 / 0.88 | n/a | n/a |
| Tamil reply draft | Correct, used only the live order data | n/a | n/a |

**Use `AI_THRESHOLD=0.7` for the demo.** The default in the code is still 0.8.
Latency: Jev took **0.6–2.9 s** per call and the chat model 1.2–1.5 s. The demo script's "under 500 ms" claim doesn't hold.

### Configuration

| Variable | Default | |
|---|---|---|
| `OPENROUTER_API_KEY` | none | Required for AI. Without it, every decision goes to review, risk returns `null`, and the reply-draft and challan endpoints return `503`. |
| `AI_THRESHOLD` | `0.8` | Use 0.7 for the demo |
| `JEV_MODEL` | `typesafe/jev-1.13` | |
| `CHAT_MODEL` | `google/gemini-2.5-flash` | Must be able to read images (for challan photos) |
| `DB_PATH` | `mill.db` | |

---

## 4. Demo script readiness

| Step | Backend | Notes |
|---|---|---|
| 1. Challan → GRN → stock → low-stock alert | ⚠️ | All working except the photo reading, which hasn't been tried with a real challan |
| 2. "Short by 120 kg" → "can start" | ✅ | Covered by the test |
| 3. Floor dashboard + Loom 4 breakdown triaged as yarn quality, high | ✅ | Needs `AI_THRESHOLD=0.7` |
| 4. Failed roll → Loom 4, night shift, operator X | ✅ | Covered by the test |
| 5. WhatsApp → routed to Production → reply drafted → sent | ⚠️ | Routing and drafting work. "Sent" only records the reply, and it takes about a second, not 500 ms. |
| 6. Ambiguous message → human queue | ✅ | Because of the `other` rule |
| 7. Dispatch scan, duplicate blocked, invoice | ✅ | Covered by the test |
| 8. Month-end costs, wages, P&L | ✅ | Covered by the test |

---

## 5. Known shortcuts

These are marked with `ponytail:` comments in the code. Each one is fine for the MVP and has a clear way to upgrade.

| Shortcut | Where | Upgrade when |
|---|---|---|
| Yarn priced at the average GRN rate | `schema.sql` (yarn stock view) | Yarn lot tracking comes in scope |
| Feasibility checks one order at a time | `app.py` feasibility | Several orders compete for the same yarn |
| ETA from the last 7 days of output | `app.py` expected-ready date | Per-stage scheduling is needed |
| Daily wage per day present | `app.py` wages | Overtime or advances come in scope |
| Money stored as rounded decimal numbers | throughout | Audited accounting is needed |
| Some list endpoints run one query per order | `/orders`, `/floor` | Hundreds of active orders |

## 6. Not built (by choice)

- **Frontend:** a camera QR scanner (rolls are scanned with a USB/Bluetooth scanner or typed), passkey and Google sign-in, and partial dispatch
- **Message delivery:** WhatsApp and email, both incoming and outgoing
- **Account admin:** user deletion and password reset; sessions never expire
- **Out of scope per the spec (§8):** purchase orders, yarn lots, partial dispatch, e-way bill, overtime, charts, voice, photo defect detection, audit log, Excel/PDF export

## 7. Next steps

1. **Commit** the frontend wiring (`frontend/`, `backend/app.py`, `backend/seed.py`, docs).
2. **Test the challan photo reading:** `cd backend && python3 smoke_ai.py challan.jpg`
3. **Decide the threshold:** keep it as a launch setting (`AI_THRESHOLD=0.7`) or change the default in the code.
4. **Replace the OpenRouter key.** It appeared in plain text in the terminal history.
5. **Run it:** `python3 seed.py` and `uvicorn app:app` in `backend/`, then `npm run dev` in `frontend/`. Sign in as `owner`.
