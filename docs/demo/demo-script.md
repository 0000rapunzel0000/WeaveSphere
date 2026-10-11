# Demo script (about 15 minutes)

Everything below was run, in this order, on a freshly built `demo.db`. Roll codes are short on purpose so they can be typed
on a phone: **ETM-1001…1003** passed and ready for a truck (order #1), **ETM-2001…2004** waiting for QC, **ETM-3001** back
from rework.

## Before you start

```bash
cd backend && rm -f demo.db demo.db-wal demo.db-shm && DB_PATH=demo.db python3 demo_setup.py   # fresh data, ~10 s
cd backend && DB_PATH=demo.db SIM_SPEED=60 python3 -m uvicorn app:app --port 8000               # backend
cd frontend && npx vite --port 5173                                                             # app: http://localhost:5173
cd whatsapp && npm start                                                                        # WhatsApp bridge (already linked)
```

Rebuild right before the demo: dates are relative to today, and at 60× speed the looms finish order #1 in about 35 minutes.
Your WhatsApp number acts as the **owner** (`BOT_PHONES` in `backend/.env`), so every skill works from the phone.
No phone? The **Assistant** page in the app is the same assistant.

## 1. The app opens (owner / everest-demo)

1. **Landing page** → scroll the story → sign in.
2. **Dashboard** — the morning digest (order #5 at risk, low polyester, 2 PO drafts, the Loom 04 / Night hotspot, an overdue
   invoice) and the live board: Loom 04 and Loom 01 weaving, metres climbing.

## 2. WhatsApp: just text it

Send these to the bot, one at a time. Wait for each reply.

| # | Send | You get |
|---|---|---|
| 1 | `Loom 04 did 120 m on night shift for order 1, Arun Kumar, 8 hours` | Done: logged (one request, saved straight away) |
| 2 | `make a roll for order 2 from loom 01, day shift, Arun` | It asks: how many metres? |
| 3 | `160 m` | Done: roll made, waiting for QC |
| 4 | `set #5 status to running, also assign Arun Kumar instead of Lakshmi, free Arun Kumar if he has other jobs` | A 3-step plan — Loom 02 running, Arun on it, Arun freed from Loom 04 and Loom 01 — for YES |
| 5 | `yes` | 1. Done … 2. Done … 3. Done … |
| 6 | `how much polyester is left and is loom 1 running?` | Two answers in one reply |
| 7 | `open a truck for order 1, TN 38 AB 1234, driver Selvam, load ETM-1001 and ETM-1002, then close it` | 4 chained steps: the truck it opens is the one it loads and closes; invoice INV-00002 |
| 8 | `book 500 m poplin in white for Meridian at 480, due 20 December, and issue 50 kg 30s cotton to it` | New order #6, and the yarn issued to *it* |
| 9 | `pass ETM-2001 and ETM-2002, reject ETM-2003 for holes near the selvedge` | 3 QC results shown for YES → `yes` (Jev classifies the reject) |
| 10 | `Meridian paid 50000 against INV-00002, NEFT 7781` | Payment recorded |
| 11 | `give me today's summary` | The morning digest |
| 12 | `thanks` | Conversation closed; the next message starts clean |

Points to make: Jev picks the skill (and how hard the message is: easy/medium/hard → which model); for several jobs the
message is split into steps; code checks every name and number; Jev checks the plan against what was said — sure → it
runs, unsure → it asks for YES. Everything goes through the same API and rules as the app.

## 3. Back in the app — the effects, and the rest

3. **Assistant** — *What the assistant did*: every WhatsApp request, its skill, level · model, Jev's match score, result.
4. **Production** — Loom 02 running with Arun; Loom 04 and Loom 01 stopped with "Arun Kumar was moved off this job". Set
   **Loom 04 → Breakdown** with a note ("belt snapped") → Jev's triage: mechanical, send the fitter.
5. **Orders** — order #2 → **YARN** (short of polyester) → **RISK** (Jev). Create an order.
6. **Inventory** — PO draft for ReThread with its reasoning → **APPROVE** → receive it in the GRN form (*Against PO*); try
   the challan photo (`docs/demo/sample-challan.jpg`); issue yarn.
7. **Quality** — PASS **ETM-2004**; reject **ETM-3001** with notes → Jev classifies it. Defect hotspots → **WE'RE ON IT**;
   the breakdown chart.
8. **Traceability** — trace **ETM-1003**: order, loom, shift, operator, yarn, QC, and **what this roll cost** (its metres
   × the order's cost per metre) → **QR LABEL** → **PRINT** (one 100 × 75 mm label); **SCAN**.
9. **Dispatch** — *Finished goods*: ETM-1003 is flagged, **waiting 12 days** on the shelf (the digest warned too: nothing
   forgotten in a corner). Truck #2 from WhatsApp is closed with its invoice. Open a new truck for order #1, load **ETM-1003**, try
   **ETM-2003** (rejected → BLOCKED), **CLOSE TRUCK & INVOICE**.
10. **Accounts** — the 50,000 from WhatsApp against INV-00002; pay the new invoice; order #4's invoice is overdue; *Cost per
    order* now ends in **cost per metre** (the cost of any roll); P&L.
11. **Inbox** — log a message for order #2 → Jev routes it → **DRAFT REPLY** in Tamil.
12. **Reviews** — settle the waiting AI decisions; the accuracy report.
13. **Workforce** — mark attendance; the register; switch to last month.
14. **Tracking** → order #2 → **OPEN BUYER PAGE** at phone width.
15. **Reports**, **Masters**.
16. **Roles** — sign out, sign in as `qc`: a short menu. On its Assistant page, `issue 10 kg of cotton to order 1` →
    "Your role can't do that".
