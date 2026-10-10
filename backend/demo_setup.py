"""A demo mill with something to show on every screen, and things left to do live. Builds on seed.py.
    cd backend && DB_PATH=demo.db python3 demo_setup.py        (needs an empty or missing demo.db)
Then run the backend on it:  DB_PATH=demo.db SIM_SPEED=60 python3 -m uvicorn app:app --port 8000
Goes through the real API (with the AI if OPENROUTER_API_KEY is set), so every rule applies.
The only shortcut: one old invoice is back-dated in SQL so the 'overdue' features have something to show."""
import os
import runpy
import sys
from datetime import date, timedelta

if not os.environ.get("DB_PATH"):
    sys.exit("Set DB_PATH (e.g. DB_PATH=demo.db) so the demo never touches mill.db.")
os.environ["SIM_SPEED"] = "1"  # build at real speed (backend/.env may say 60); the demo server runs at demo speed later
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed.py"), run_name="seed")  # base data, empty DB only

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from mill import db as mdb, livefloor  # noqa: E402

c = TestClient(app.app)
TODAY = date.today()
ago = lambda n: (TODAY - timedelta(days=n)).isoformat()  # noqa: E731


def H(user):
    return {"Authorization": "Bearer " + c.post("/auth/login", json={"username": user, "password": os.environ.get("DEMO_PASSWORD", "everest-demo")}).json()["token"]}


O, ST, FL, QC = H("owner"), H("store"), H("floor"), H("qc")


def ok(r):
    if r.status_code >= 400:
        sys.exit(f"{r.request.method} {r.request.url.path} failed: {r.status_code} {r.text[:300]}")
    return r.json()


post = lambda path, body, h=O: ok(c.post(path, json=body, headers=h))  # noqa: E731
step = lambda s: print("·", s, flush=True)  # noqa: E731

# Seed ids: buyers 1 Meridian, 2 North Loom · suppliers 1 South Cotton, 2 ReThread · yarns 1 cotton 30s, 2 polyester 24s,
# 3 cotton 20s · fabrics 1 poplin, 2 twill, 3 canvas · machines 1 Spinning 01, 2 Loom 01, 3 Loom 04, 4 Dye line 01 ·
# workers 1 Arun (piece), 2 Meena, 3 Ravi · shifts 1 Day, 2 Night · orders 1 poplin (Loom 04 running), 2 twill, 3 canvas

step("more masters: a third buyer, a fourth worker, a second loom")
kids = post("/master/buyers", {"name": "Coimbatore Kids Wear", "phone": "+91 99420 33771", "email": "buy@cbekids.example", "gstin": "33AAFCC5521K1Z2"})["id"]
lakshmi = post("/master/workers", {"name": "Lakshmi R.", "wage_type": "daily", "rate": 640})["id"]
loom2 = post("/master/machines", {"name": "Loom 02", "kind": "weaving", "overhead_per_hour": 210, "rate_m_per_hour": 12})["id"]

step("a finished, fully shipped order (#4) with an invoice that is now overdue")
o4 = post("/orders", {"buyer_id": 2, "fabric_type_id": 3, "colour": "sand", "qty_m": 400, "deadline": ago(20), "price_per_m": 600})["id"]
post("/issues", {"order_id": o4, "yarn_type_id": 3, "qty_kg": 116, "machine_id": 2}, ST)
for d in (48, 47, 46, 45):
    post("/production", {"entry_date": ago(d), "shift_id": 1, "machine_id": 2, "order_id": o4, "operator_id": 3, "output": 100, "hours": 8}, FL)
rolls4 = [post("/rolls", {"order_id": o4, "machine_id": 2, "shift_id": 1, "operator_id": 3, "metres": 100}, FL)["qr"] for _ in range(4)]
for q in rolls4:
    post(f"/qc/{q}", {"result": "pass"}, QC)
truck = post("/dispatches", {"order_id": o4, "vehicle_no": "TN 37 CK 2210", "driver": "Murugan · 98431 20117"}, ST)["id"]
for q in rolls4:
    post(f"/dispatches/{truck}/scan", {"qr": q}, ST)
inv4 = post(f"/dispatches/{truck}/close", {}, ST)["invoice"]
db = mdb.connect()
db.execute("UPDATE invoices SET issued_on = ? WHERE id = ?", (ago(42), inv4["id"]))  # the one SQL shortcut: make it overdue
db.close()
post("/payments", {"party": "buyer", "party_id": 2, "amount": 100000, "invoice_id": inv4["id"], "paid_on": ago(30), "ref": "NEFT 88213"})

step("order #1: two more rolls woven and passed, ready for a truck")
ready = [post("/rolls", {"order_id": 1, "machine_id": 3, "shift_id": 1, "operator_id": 2, "metres": 150}, FL)["qr"] for _ in range(2)]
for q in ready:
    post(f"/qc/{q}", {"result": "pass"}, QC)

step("a defect hotspot on Loom 04 / Night (rejects with notes, classified by Jev)")
notes = ["bars across the width, uneven pick density", "loose and tight patches, irregular weave",
         "thick and thin places across the fabric", "pattern fault, weave density changes every few metres"]
for n in notes:
    q = post("/rolls", {"order_id": 1, "machine_id": 3, "shift_id": 2, "operator_id": 1, "metres": 40}, FL)["qr"]
    post(f"/qc/{q}", {"result": "reject", "notes": n}, QC)
q = post("/rolls", {"order_id": 1, "machine_id": 3, "shift_id": 2, "operator_id": 1, "metres": 40}, FL)["qr"]
post(f"/qc/{q}", {"result": "pass"}, QC)
q = post("/rolls", {"order_id": 1, "machine_id": 3, "shift_id": 2, "operator_id": 1, "metres": 40}, FL)["qr"]
post(f"/qc/{q}", {"result": "rework", "defect_type": "stain", "notes": "oil spot near one edge"}, QC)

step("order #2 (North Loom twill) under way on Loom 01: yarn issued, a week of output")
post("/issues", {"order_id": 2, "yarn_type_id": 2, "qty_kg": 60, "machine_id": 2}, ST)
post("/issues", {"order_id": 2, "yarn_type_id": 1, "qty_kg": 20, "machine_id": 2}, ST)
for d, m in ((6, 70), (5, 80), (4, 75), (2, 85), (1, 90)):
    post("/production", {"entry_date": ago(d), "shift_id": 1, "machine_id": 2, "order_id": 2, "operator_id": 1, "output": m, "hours": 8}, FL)
post("/jobs", {"order_id": 2, "machine_id": 2, "operator_id": 1, "shift_id": 1}, FL)
post("/machines/2/status", {"status": "Running"}, FL)
post("/rolls", {"order_id": 2, "machine_id": 2, "shift_id": 1, "operator_id": 1, "metres": 160}, FL)  # waits for QC

step("order #5: a rush order for the new buyer, deadline in 4 days, on Loom 02")
o5 = post("/orders", {"buyer_id": kids, "fabric_type_id": 1, "colour": "sky blue", "qty_m": 350, "deadline": (TODAY + timedelta(days=4)).isoformat(), "price_per_m": 495})["id"]
post("/issues", {"order_id": o5, "yarn_type_id": 1, "qty_kg": 30, "machine_id": loom2}, ST)
post("/production", {"entry_date": ago(1), "shift_id": 2, "machine_id": loom2, "order_id": o5, "operator_id": lakshmi, "output": 40, "hours": 8}, FL)
post("/jobs", {"order_id": o5, "machine_id": loom2, "operator_id": lakshmi, "shift_id": 2}, FL)

step("a breakdown last week on Loom 01 (triaged by Jev), since repaired")
db = mdb.connect()
r = post("/machines/2/status", {"status": "Breakdown", "note": "warp threads keep snapping near the back rest, motor sounds fine"}, FL)
db.execute("UPDATE machine_status_log SET at = datetime('now', '-6 days') WHERE id = ?", (r["log"]["id"],))
db.close()
post("/machines/2/status", {"status": "Running"}, FL)

step("the looms have been running: Loom 04 for 20 hours, Loom 01 for 6 (machine time)")
db = mdb.connect()
db.execute("UPDATE job_cards SET last_tick = datetime('now','-20 hours'), running_since = datetime('now','-20 hours') WHERE machine_id = 3 AND status = 'open'")
db.execute("UPDATE job_cards SET last_tick = datetime('now','-6 hours'), running_since = datetime('now','-6 hours') WHERE machine_id = 2 AND status = 'open'")
db.close()
livefloor._last_accrual = 0
ok(c.get("/floor", headers=O))

step("three weeks of attendance (Sundays off, a few absences)")
staff = [1, 2, 3, lakshmi]
for d in range(21, 0, -1):
    day = TODAY - timedelta(days=d)
    if day.weekday() == 6:
        continue
    present = [w for i, w in enumerate(staff) if (d + i) % 9]  # someone is off now and then
    post("/attendance", {"shift_id": 1, "work_date": day.isoformat(), "worker_ids": present[:2]}, FL)
    post("/attendance", {"shift_id": 2, "work_date": day.isoformat(), "worker_ids": present[2:] + ([1] if d % 5 == 0 else [])}, FL)

step("supplier payments")
post("/payments", {"party": "supplier", "party_id": 1, "amount": 250000, "paid_on": ago(12), "ref": "RTGS 55102"})
post("/payments", {"party": "supplier", "party_id": 2, "amount": 15000, "paid_on": ago(5), "ref": "UPI 7781"})

step("buyer messages, routed by Jev (one is unclear, for the review queue)")
for m in [{"text": "Hi, any update on the navy twill? Our cutting starts on the 2nd and we can't slip.", "order_id": 2},
          {"text": "Received the poplin invoice. Can you resend it with our new GSTIN 33AAACM1234F1Z5?", "order_id": 1},
          {"text": "The last canvas lot had a few rolls with uneven weave near the selvedge. Please check before the next dispatch.", "buyer_id": 2},
          {"text": "What fabrics do you make? Looking for about 3,000 m of canvas for bags.", "channel": "email"},
          {"text": "ok noted, thanks", "buyer_id": kids}]:
    r = post("/messages", m)
    print(f"    routed: {r['message']['status']:7} {r['message']['dept'] or '-':10} {m['text'][:50]}")

step("the morning digest")
d = post("/digest/refresh", {})
print("   ", d["source"], "|", d["summary"][:160])

db = mdb.connect()
q = lambda s: db.execute(s).fetchall()  # noqa: E731
print("\nReady:")
print("  orders:", ", ".join(f"#{o} {s}" for o, s in [(o["id"], o["stage"]) for o in ok(c.get("/orders", headers=O))]))
print("  rolls waiting for QC:", ", ".join(r[0] for r in q("SELECT qr FROM rolls WHERE status IN ('pending_qc','rework') ORDER BY id")))
print("  passed rolls ready for a truck:", ", ".join(r[0] for r in q("SELECT qr FROM rolls WHERE status='passed' ORDER BY id")))
print("  PO drafts:", len(q("SELECT 1 FROM purchase_orders WHERE status='draft'")), "| live defect alerts:", len(q("SELECT 1 FROM defect_alerts WHERE status='open'")),
      "| AI reviews waiting:", len(q("SELECT 1 FROM ai_reviews WHERE status='open'")))
print("  buyer links:", "  ".join(f"#{i}: /track.html#track={t}" for i, t in q("SELECT id, token FROM orders ORDER BY id")))
db.close()
