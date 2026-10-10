"""Walks the demo script end to end. Run from backend/: python3 -m pytest -q tests"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["OPENROUTER_API_KEY"] = ""  # blank, not unset: stops mill/config.py loading the real key from .env
# Pin settings that backend/.env may set for demos, so results never depend on that file.
os.environ.update(SIM_SPEED="1", AI_THRESHOLD="0.8")

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from mill import ai, auth, inventory, livefloor  # noqa: E402
from mill.db import connect  # noqa: E402

c = TestClient(app.app)


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def test_demo_flow():
    owner = {"Authorization": "Bearer " + ok(c.post("/auth/setup", json={"username": "boss", "password": "secret123"}))["token"]}
    assert c.post("/auth/setup", json={"username": "x", "password": "secret123"}).status_code == 403
    ok(c.post("/users", json={"username": "qc1", "password": "secret123", "role": "qc"}, headers=owner))
    qc = {"Authorization": "Bearer " + ok(c.post("/auth/login", json={"username": "qc1", "password": "secret123"}))["token"]}
    assert c.post("/orders", json={}, headers=qc).status_code in (403, 422)
    assert c.get("/floor").status_code == 401

    m = lambda t, d: ok(c.post(f"/master/{t}", json=d, headers=owner))["id"]  # noqa: E731
    buyer = m("buyers", {"name": "Acme Garments", "gstin": "33ABC"})
    other_buyer = m("buyers", {"name": "Other"})
    sup = m("suppliers", {"name": "Sri Yarns"})
    yarn = m("yarn_types", {"count": "40s", "material": "cotton", "colour": "white", "reorder_kg": 100})
    fab = m("fabric_types", {"name": "Poplin", "gst_rate": 5})
    m("bom", {"fabric_type_id": fab, "yarn_type_id": yarn, "kg_per_m": 0.2})
    loom = m("machines", {"name": "Loom 4", "kind": "weaving", "overhead_per_hour": 50})
    night = m("shifts", {"name": "Night"})
    op = m("workers", {"name": "Operator X", "wage_type": "piece", "rate": 2})
    daily = m("workers", {"name": "Helper", "wage_type": "daily", "rate": 500})
    assert c.post("/master/machines", json={"name": "L5", "kind": "weaving", "status": "Running"}, headers=owner).status_code == 422
    dup = c.post("/master/machines", json={"name": "Loom 4", "kind": "weaving"}, headers=owner)
    assert dup.status_code == 409 and dup.json()["detail"].startswith("Already exists")
    assert c.post("/master/machines", json={"name": "L9", "kind": "knitting"}, headers=owner).status_code == 422
    missing = c.post("/orders", json={"colour": "red"}, headers=owner)
    assert missing.status_code == 422 and isinstance(missing.json()["detail"], str)  # always one readable line
    assert c.post("/master/users", json={}, headers=owner).status_code == 404

    # 1. Yarn arrives, low-stock alert fires
    r = ok(c.post("/grn", json={"supplier_id": sup, "yarn_type_id": yarn, "qty_kg": 80, "rate": 300}, headers=owner))
    assert r["stock"]["stock_kg"] == 80 and r["low_stock"][0]["id"] == yarn

    # 2. New order: short by 120 kg, then can start once restocked
    order = ok(c.post("/orders", json={"buyer_id": buyer, "fabric_type_id": fab, "colour": "navy", "qty_m": 1000,
                                       "deadline": "2099-01-01", "price_per_m": 100}, headers=owner))
    oid = order["id"]
    assert order["feasibility"]["verdict"] == "short by 120 kg" and order["stage"] == "Pending"
    ok(c.post("/grn", json={"supplier_id": sup, "yarn_type_id": yarn, "qty_kg": 120, "rate": 300}, headers=owner))
    assert ok(c.get(f"/orders/{oid}/feasibility", headers=owner))["verdict"] == "can start"
    # A later order for the same yarn can't count stock the first order still needs.
    probe = ok(c.post("/orders", json={"buyer_id": buyer, "fabric_type_id": fab, "colour": "red", "qty_m": 100,
                                       "deadline": "2099-01-01", "price_per_m": 100}, headers=owner))
    line = probe["feasibility"]["lines"][0]
    assert (line["available_kg"], line["reserved_kg"], line["free_kg"]) == (200, 200, 0)
    assert probe["feasibility"]["verdict"] == "short by 20 kg"
    assert ok(c.get(f"/orders/{oid}/feasibility", headers=owner))["verdict"] == "can start"  # earlier order unaffected
    assert c.post("/issues", json={"order_id": oid, "yarn_type_id": yarn, "qty_kg": 999}, headers=owner).status_code == 409
    ok(c.post("/issues", json={"order_id": oid, "yarn_type_id": yarn, "qty_kg": 200}, headers=owner))

    # 3. Floor: job, breakdown triage (confident Jev -> auto-applied)
    ok(c.post("/jobs", json={"order_id": oid, "machine_id": loom}, headers=owner))
    ok(c.post(f"/machines/{loom}/status", json={"status": "Running"}, headers=owner))
    ok(c.post("/production", json={"shift_id": night, "machine_id": loom, "order_id": oid, "operator_id": op,
                                   "output": 1000, "hours": 10}, headers=owner))
    ok(c.post("/production", json={"shift_id": night, "machine_id": loom, "order_id": oid, "operator_id": daily,
                                   "output": 0, "hours": 8}, headers=owner))
    real = ai.decide
    ai.decide = lambda state, qs: {n: {"choice": next(iter(opts)), "probs": {}, "confidence": 1.0}
                                   for n, (_, opts) in qs.items()}
    try:
        t = ok(c.post(f"/machines/{loom}/status", json={"status": "Breakdown", "note": "yarn keeps snapping"}, headers=owner))
        assert t["triage"]["auto"] and t["log"]["category"] == "mechanical"
        assert t["log"]["assignee"] == "fitter"  # by rule, not by Jev
        msg = ok(c.post("/messages", json={"text": "where is my order?", "order_id": oid}, headers=owner))
        assert msg["message"]["status"] == "routed" and msg["message"]["intent"] == "status"
    finally:
        ai.decide = real
    # Auto-applied decisions are logged and can still be corrected; corrections feed the accuracy report.
    settled = ok(c.get("/reviews?status=resolved&kind=breakdown", headers=owner))
    assert settled[0]["auto"] and settled[0]["id"] == t["triage"]["review_id"]
    assert c.post(f"/reviews/{t['triage']['review_id']}", headers=owner).status_code == 409  # nothing to change
    ok(c.post(f"/reviews/{t['triage']['review_id']}", json={"category": "electrical"}, headers=owner))
    assert ok(c.get(f"/machines/{loom}/log", headers=owner))[0]["assignee"] == "electrician"
    acc = ok(c.get("/reviews/accuracy", headers=owner))["kinds"]["breakdown"]
    assert (acc["auto_applied"], acc["bands"]["0.9-1.0"]) == (1, {"checked": 1, "agreed": 0, "accuracy": 0.0})
    floor = ok(c.get("/floor", headers=owner))
    assert floor["machines"][0]["order_id"] == oid and floor["machines"][0]["idle"]
    assert floor["machines"][0]["today_output"] == 1000
    o = ok(c.get(f"/orders/{oid}", headers=owner))
    assert o["stage"] == "Weaving"
    assert (o["produced_m"], o["progress_pct"], o["rolls_produced"]) == (1000, 100.0, 0)  # loom output counts before rolls
    assert o["expected_ready"] is not None

    # 4. QC: one roll fails, trace shows Loom 4, night shift, operator X; one passes
    roll = lambda: ok(c.post("/rolls", json={"order_id": oid, "machine_id": loom, "shift_id": night,  # noqa: E731
                                             "operator_id": op, "metres": 500}, headers=owner))["qr"]
    bad, good, good2 = roll(), roll(), roll()
    assert len(ok(c.get("/qc/queue", headers=qc))) == 3
    assert c.post(f"/qc/{bad}", json={"result": "reject"}, headers=qc).status_code == 422
    r = ok(c.post(f"/qc/{bad}", json={"result": "reject", "notes": "holes along the selvedge"}, headers=qc))
    assert not r["classification"]["auto"]  # no AI configured -> human review queue
    review = ok(c.get("/reviews", headers=qc))[0]
    assert c.post(f"/reviews/{review['id']}", json={"defect_type": "nope"}, headers=qc).status_code == 422
    ok(c.post(f"/reviews/{review['id']}", json={"defect_type": "hole", "severity": "high", "stage_at_fault": "weaving"}, headers=qc))
    tr = ok(c.get(f"/rolls/{bad}", headers=qc))
    assert (tr["machine"], tr["shift"], tr["operator"], tr["status"]) == ("Loom 4", "Night", "Operator X", "rejected")
    assert tr["qc_history"][0]["defect_type"] == "hole" and tr["yarn_issued"][0]["qty_kg"] == 200
    ok(c.post(f"/qc/{good}", json={"result": "rework", "defect_type": "stain"}, headers=qc))
    ok(c.post(f"/qc/{good}", json={"result": "pass"}, headers=qc))  # rework loop back through QC
    ok(c.post(f"/qc/{good2}", json={"result": "pass"}, headers=qc))
    assert c.post(f"/qc/{good}", json={"result": "pass"}, headers=qc).status_code == 409

    # 5/6. Buyer tracking link, ambiguous message goes to the human queue
    track = ok(c.get(order["tracking_path"]))
    assert track["stage"] == "Ready" and track["rolls_passed"] == 2 and track["passed_m"] == 1000
    assert c.get("/track/wrong").status_code == 404
    # A message can't pair a buyer with someone else's order; an order alone fills in its buyer.
    assert c.post("/messages", json={"text": "x", "buyer_id": other_buyer, "order_id": oid}, headers=owner).status_code == 422
    seen = {}
    real_draft = ai.draft_reply
    ai.draft_reply = lambda text, data, lang: seen.update(data) or "ok"
    try:
        m = ok(c.post("/messages", json={"text": "when is the red order ready?", "order_id": oid}, headers=owner))["message"]
        assert m["buyer_id"] == buyer
        ok(c.post(f"/messages/{m['id']}/draft", json={"lang": "en"}, headers=owner))
        assert [o["order_no"] for o in seen["orders"]][:2] == [oid, probe["id"]]  # logged order first, then the rest
    finally:
        ai.draft_reply = real_draft
    amb = ok(c.post("/messages", json={"text": "hmm", "buyer_id": buyer}, headers=owner))
    assert amb["message"]["status"] == "review"
    rv = [r for r in ok(c.get("/reviews", headers=owner)) if r["kind"] == "message"][0]
    assert ok(c.post(f"/reviews/{rv['id']}", headers=owner))["applied"]["dept"] == "Office"  # empty body = accept
    assert c.post(f"/reviews/{rv['id']}", headers=owner).status_code == 409
    assert ok(c.get("/messages?status=routed", headers=owner))

    # 7. Dispatch: guards, duplicate scan blocked, invoice generated
    other = ok(c.post("/orders", json={"buyer_id": other_buyer, "fabric_type_id": fab, "colour": "red", "qty_m": 10,
                                       "deadline": "2099-01-01", "price_per_m": 1}, headers=owner))
    wrong = ok(c.post("/dispatches", json={"order_id": other["id"], "vehicle_no": "TN01", "driver": "Ravi"}, headers=owner))
    assert "belongs to order" in c.post(f"/dispatches/{wrong['id']}/scan", json={"qr": good}, headers=owner).json()["detail"]
    d = ok(c.post("/dispatches", json={"order_id": oid, "vehicle_no": "TN02", "driver": "Kumar"}, headers=owner))
    assert "not QC-passed" in c.post(f"/dispatches/{d['id']}/scan", json={"qr": bad}, headers=owner).json()["detail"]
    ok(c.post(f"/dispatches/{d['id']}/scan", json={"qr": good}, headers=owner))
    dup = c.post(f"/dispatches/{d['id']}/scan", json={"qr": good}, headers=owner)
    assert dup.status_code == 409 and "already scanned" in dup.json()["detail"]
    ok(c.post(f"/dispatches/{d['id']}/scan", json={"qr": good2}, headers=owner))
    inv = ok(c.post(f"/dispatches/{d['id']}/close", headers=owner))["invoice"]
    assert (inv["subtotal"], inv["gst"], inv["total"]) == (100000, 5000, 105000)
    assert ok(c.get(order["tracking_path"]))["stage"] == "Dispatched"
    ok(c.post("/payments", json={"party": "buyer", "party_id": buyer, "invoice_id": inv["id"], "amount": 5000}, headers=owner))
    assert c.post("/payments", json={"party": "buyer", "party_id": other_buyer, "invoice_id": inv["id"], "amount": 1},
                  headers=owner).status_code == 422
    assert ok(c.get("/reports/receivables", headers=owner))[0]["outstanding"] == 100000

    # 8. Month-end
    ok(c.post("/attendance", json={"shift_id": night, "worker_ids": [op, daily, daily]}, headers=owner))
    w = ok(c.get("/wages", headers=owner))
    assert w["total"] == 1000 * 2 + 500
    cost = ok(c.get(f"/reports/order-cost/{oid}", headers=owner))
    # yarn 200kg x 300, wages 2000 piece + 500 daily (all his hours on this order), overhead 18h x 50
    assert (cost["yarn"], cost["wages"], cost["overhead"]) == (60000, 2500, 900)
    p = ok(c.get("/reports/pnl", headers=owner))
    assert (p["revenue"], p["total_costs"], p["profit"]) == (100000, 63400, 36600)
    assert c.get("/reports/pnl", headers=qc).status_code == 403
    assert ok(c.get("/reports/payables", headers=owner))[0]["outstanding"] == 60000

    # A pricier delivery later must not rewrite costs already booked (rate is captured at issue time).
    ok(c.post("/grn", json={"supplier_id": sup, "yarn_type_id": yarn, "qty_kg": 50, "rate": 1000}, headers=owner))
    assert ok(c.get(f"/reports/order-cost/{oid}", headers=owner))["yarn"] == 60000
    assert ok(c.get("/reports/pnl", headers=owner))["total_costs"] == 63400
    s = [y for y in ok(c.get("/stock", headers=owner)) if y["id"] == yarn][0]
    assert (s["stock_kg"], s["avg_rate"]) == (50, 1000)  # moving average of what is on hand


def owner_headers():
    r = c.post("/auth/setup", json={"username": "boss", "password": "secret123"})
    if r.status_code == 403:
        r = c.post("/auth/login", json={"username": "boss", "password": "secret123"})
    return {"Authorization": "Bearer " + ok(r)["token"]}


def test_hardening():
    h = owner_headers()
    # Sign-in: unknown users and wrong passwords look the same; 5 failures lock the name for a while.
    ok(c.post("/users", json={"username": "lock1", "password": "secret123", "role": "qc"}, headers=h))
    assert c.post("/auth/login", json={"username": "nobody", "password": "secret123"}).status_code == 401
    for _ in range(5):
        assert c.post("/auth/login", json={"username": "lock1", "password": "wrong-pass"}).status_code == 401
    assert c.post("/auth/login", json={"username": "lock1", "password": "secret123"}).status_code == 429
    assert c.post("/users", json={"username": "has space", "password": "secret123", "role": "qc"}, headers=h).status_code == 422
    # JWTs: expired, forged, unsigned and signed-out tokens are all refused.
    import time
    import jwt
    claims = {"sub": "1", "jti": "t", "exp": int(time.time()) + 60}
    bearer = lambda t: {"Authorization": "Bearer " + t}  # noqa: E731
    assert c.get("/me", headers=bearer(jwt.encode({**claims, "exp": int(time.time()) - 1}, auth.jwt_secret()))).status_code == 401
    assert c.get("/me", headers=bearer(jwt.encode(claims, "x" * 48))).status_code == 401
    assert c.get("/me", headers=bearer(jwt.encode(claims, None, algorithm="none"))).status_code == 401
    assert c.get("/me", headers=bearer("garbage")).status_code == 401
    out = ok(c.post("/auth/login", json={"username": "boss", "password": "secret123"}))["token"]
    assert ok(c.get("/me", headers=bearer(out)))["username"] == "boss"
    ok(c.post("/auth/logout", headers=bearer(out)))
    assert c.get("/me", headers=bearer(out)).status_code == 401
    assert c.get("/me", headers=h).status_code == 200  # other sign-ins are unaffected

    m = lambda t, d: ok(c.post(f"/master/{t}", json=d, headers=h))["id"]  # noqa: E731
    buyer, sup = m("buyers", {"name": "Part Ship"}), m("suppliers", {"name": "Odd Lots"})
    yarn = m("yarn_types", {"count": "12s", "material": "jute", "colour": "raw"})
    fab = m("fabric_types", {"name": "Hessian"})
    loom = m("machines", {"name": "Loom H", "kind": "weaving"})
    shift, op = m("shifts", {"name": "H shift"}), m("workers", {"name": "H op", "wage_type": "daily", "rate": 1})

    # Floating-point: issuing exactly what is on hand (0.1 + 0.2 kg) works.
    for q in (0.1, 0.2):
        ok(c.post("/grn", json={"supplier_id": sup, "yarn_type_id": yarn, "qty_kg": q, "rate": 10}, headers=h))
    oid = ok(c.post("/orders", json={"buyer_id": buyer, "fabric_type_id": fab, "colour": "raw", "qty_m": 1000,
                                     "deadline": "2099-01-01", "price_per_m": 1}, headers=h))["id"]
    ok(c.post("/issues", json={"order_id": oid, "yarn_type_id": yarn, "qty_kg": 0.3}, headers=h))
    assert c.post("/orders", json={"buyer_id": buyer, "fabric_type_id": fab, "colour": "", "qty_m": 1,
                                   "deadline": "2099-01-01", "price_per_m": 1}, headers=h).status_code == 422

    # Partial dispatch: half shipped is not "Dispatched"; all shipped is.
    def passed_roll():
        qr = ok(c.post("/rolls", json={"order_id": oid, "machine_id": loom, "shift_id": shift, "operator_id": op,
                                        "metres": 500}, headers=h))["qr"]
        q = ok(c.post(f"/qc/{qr}", json={"result": "pass", "defect_type": "stain"}, headers=h))["qc"]
        assert q["defect_type"] is None  # a pass carries no defect
        return qr

    def ship(qr):
        d = ok(c.post("/dispatches", json={"order_id": oid, "vehicle_no": "TN", "driver": "D"}, headers=h))["id"]
        ok(c.post(f"/dispatches/{d}/scan", json={"qr": qr}, headers=h))
        ok(c.post(f"/dispatches/{d}/close", headers=h))

    ship(passed_roll())
    o = ok(c.get(f"/orders/{oid}", headers=h))
    assert (o["stage"], o["dispatched_m"]) == ("Pending", 500)
    ship(passed_roll())
    assert ok(c.get(f"/orders/{oid}", headers=h))["stage"] == "Dispatched"

    # Payments must name a real buyer/supplier; challan uploads must be images.
    assert c.post("/payments", json={"party": "supplier", "party_id": 99999, "amount": 1}, headers=h).status_code == 422
    assert c.post("/grn/challan", json={"image_b64": "x", "mime": "text/html"}, headers=h).status_code == 422


def test_live_floor():
    """A running loom weaves at its speed, uses issued yarn then store yarn, stops when out, and closes the job when done."""
    h = owner_headers()
    m = lambda t, d: ok(c.post(f"/master/{t}", json=d, headers=h))["id"]  # noqa: E731
    buyer, sup = m("buyers", {"name": "Live"}), m("suppliers", {"name": "Live Yarns"})
    yarn = m("yarn_types", {"count": "8s", "material": "linen", "colour": "flax"})
    fab = m("fabric_types", {"name": "Linen sheet"})
    m("bom", {"fabric_type_id": fab, "yarn_type_id": yarn, "kg_per_m": 0.5})
    loom = m("machines", {"name": "Loom L", "kind": "weaving", "rate_m_per_hour": 10, "overhead_per_hour": 100})
    shift, op = m("shifts", {"name": "L shift"}), m("workers", {"name": "L op", "wage_type": "piece", "rate": 3})
    ok(c.post("/grn", json={"supplier_id": sup, "yarn_type_id": yarn, "qty_kg": 20, "rate": 100}, headers=h))
    oid = ok(c.post("/orders", json={"buyer_id": buyer, "fabric_type_id": fab, "colour": "flax", "qty_m": 100,
                                     "deadline": "2099-01-01", "price_per_m": 1}, headers=h))["id"]
    ok(c.post("/issues", json={"order_id": oid, "yarn_type_id": yarn, "qty_kg": 5}, headers=h))  # 5 kg at the loom
    ok(c.post("/jobs", json={"order_id": oid, "machine_id": loom}, headers=h))
    r = c.post(f"/machines/{loom}/status", json={"status": "Running"}, headers=h)  # nobody to credit the output to
    assert r.status_code == 422 and "operator and shift" in r.json()["detail"]
    ok(c.post("/jobs", json={"order_id": oid, "machine_id": loom, "operator_id": op, "shift_id": shift}, headers=h))
    ok(c.post(f"/machines/{loom}/status", json={"status": "Running"}, headers=h))

    def run_for(hours):
        db = connect()
        db.execute("UPDATE job_cards SET last_tick = datetime('now', ?) WHERE machine_id = ? AND status = 'open'",
                   (f"-{hours} hours", loom))
        db.close()
        livefloor._last_accrual = 0
        return [x for x in ok(c.get("/floor", headers=h))["machines"] if x["id"] == loom][0]

    stock = lambda: [y for y in ok(c.get("/stock", headers=h)) if y["id"] == yarn][0]["stock_kg"]  # noqa: E731
    f = run_for(2)  # 20 m needs 10 kg: 5 kg already at the loom + 5 kg drawn from the store
    assert (round(f["stage_done_m"], 1), f["status"], f["yarn_kg_per_hour"]) == (20.0, "Running", 5.0)
    assert round(stock(), 1) == 10.0 and f["finishes_at"]
    o = ok(c.get(f"/orders/{oid}", headers=h))
    assert (o["produced_m"], o["progress_pct"], o["stage"]) == (20.0, 20.0, "Weaving")

    f = run_for(10)  # wants 100 m but only 10 kg left in the store = 20 m more, then it stops
    assert (round(f["stage_done_m"], 1), f["status"], round(stock(), 3)) == (40.0, "Idle", 0)
    assert "out of 8s linen flax" in ok(c.get(f"/machines/{loom}/log", headers=h))[0]["note"]

    ok(c.post("/grn", json={"supplier_id": sup, "yarn_type_id": yarn, "qty_kg": 100, "rate": 100}, headers=h))
    ok(c.post(f"/machines/{loom}/status", json={"status": "Running"}, headers=h))
    f = run_for(100)  # far more than needed: finishes the remaining 60 m and closes the job
    assert (f["status"], f["job_id"], round(stage_done := ok(c.get(f"/orders/{oid}", headers=h))["produced_m"], 1)) \
        == ("Idle", None, 100.0), stage_done
    assert "finished" in ok(c.get(f"/machines/{loom}/log", headers=h))[0]["note"]
    assert round(stock(), 1) == 70.0  # 100 m x 0.5 kg = 50 kg used in all: 5 issued + 45 from the store
    cost = ok(c.get(f"/reports/order-cost/{oid}", headers=h))
    assert (cost["wages"], cost["overhead"]) == (300.0, 1000.0)  # 100 m x 3 piece rate; 10 loom hours x 100


def test_last_yarn_cannot_be_issued_twice():
    """Two people issue the last 100 kg at the same moment: exactly one wins, stock never goes negative.
    The stock read is slowed down so both requests are inside the check at once; only the write lock saves it."""
    import threading
    import time

    db = connect()
    sid = db.execute("INSERT INTO suppliers (name) VALUES ('race')").lastrowid
    yid = db.execute("INSERT INTO yarn_types (count, material, colour) VALUES ('99s', 'copper', 'x')").lastrowid
    bid = db.execute("INSERT INTO buyers (name) VALUES ('race')").lastrowid
    fid = db.execute("INSERT INTO fabric_types (name) VALUES ('race')").lastrowid
    oids = [db.execute("INSERT INTO orders (buyer_id, fabric_type_id, colour, qty_m, deadline, price_per_m, token) "
                       "VALUES (?, ?, 'x', 1, '2099-01-01', 1, ?)", (bid, fid, f"race{i}")).lastrowid for i in (1, 2)]
    real_one = inventory.one

    def slow_one(conn, sql, *args):
        r = real_one(conn, sql, *args)
        if "yarn_stock" in sql:
            time.sleep(0.05)
        return r

    inventory.one = slow_one
    try:
        for _ in range(5):
            db.execute("INSERT INTO grn (supplier_id, yarn_type_id, qty_kg, rate) VALUES (?, ?, 100, 200)", (sid, yid))
            start, results = threading.Barrier(2), []

            def issue(order_id):
                conn = connect()
                start.wait()
                try:
                    inventory.issue_yarn(inventory.IssueIn(order_id=order_id, yarn_type_id=yid, qty_kg=100), db=conn, u={"id": None})
                    results.append("ok")
                except HTTPException as e:
                    results.append(e.status_code)
                finally:
                    conn.close()

            threads = [threading.Thread(target=issue, args=(o,)) for o in oids]
            [th.start() for th in threads]
            [th.join() for th in threads]
            assert sorted(results, key=str) == [409, "ok"], results
            assert db.execute("SELECT stock_kg FROM yarn_stock WHERE id = ?", (yid,)).fetchone()[0] == 0
    finally:
        inventory.one = real_one
        db.close()


if __name__ == "__main__":
    test_demo_flow()
    test_hardening()
    test_live_floor()
    test_last_yarn_cannot_be_issued_twice()
    print("ok")


def test_po_drafts_and_defect_alerts():
    """Shortfall -> draft PO from the cheapest supplier; receiving it clears the need. 5 straight QC fails -> one alert."""
    h = owner_headers()
    m = lambda t, d: ok(c.post(f"/master/{t}", json=d, headers=h))["id"]  # noqa: E731
    buyer, cheap, dear = m("buyers", {"name": "PO buyer"}), m("suppliers", {"name": "Cheap"}), m("suppliers", {"name": "Dear"})
    yarn = m("yarn_types", {"count": "60s", "material": "silk", "colour": "white", "reorder_kg": 10})
    fab = m("fabric_types", {"name": "Silk"})
    m("bom", {"fabric_type_id": fab, "yarn_type_id": yarn, "kg_per_m": 0.5})
    ok(c.post("/grn", json={"supplier_id": dear, "yarn_type_id": yarn, "qty_kg": 10, "rate": 900}, headers=h))
    ok(c.post("/grn", json={"supplier_id": cheap, "yarn_type_id": yarn, "qty_kg": 10, "rate": 700}, headers=h))
    oid = ok(c.post("/orders", json={"buyer_id": buyer, "fabric_type_id": fab, "colour": "white", "qty_m": 100,
                                     "deadline": "2099-01-01", "price_per_m": 1}, headers=h))["id"]
    po = [p for p in ok(c.get("/purchase-orders?status=draft", headers=h)) if p["lines"][0]["yarn_type_id"] == yarn][0]
    line = po["lines"][0]
    assert po["supplier_id"] == cheap and line["qty_kg"] == 40 and line["rate"] == 700  # 50 needed + 10 reserve - 20 stock
    grn = {"supplier_id": cheap, "yarn_type_id": yarn, "qty_kg": 40, "rate": 700, "po_line_id": line["id"]}
    assert c.post("/grn", json=grn, headers=h).status_code == 409  # not approved yet
    ok(c.post(f"/purchase-orders/{po['id']}/approve", headers=h))
    assert c.post("/grn", json={**grn, "supplier_id": dear}, headers=h).status_code == 409  # wrong supplier
    ok(c.post("/grn", json=grn, headers=h))
    assert ok(c.get(f"/purchase-orders/{po['id']}", headers=h))["status"] == "received"
    assert not [p for p in ok(c.get("/purchase-orders?status=draft", headers=h)) if p["lines"][0]["yarn_type_id"] == yarn]

    loom, shift = m("machines", {"name": "Loom D", "kind": "weaving"}), m("shifts", {"name": "D shift"})
    op = m("workers", {"name": "D op", "wage_type": "daily", "rate": 1})
    roll = lambda: ok(c.post("/rolls", json={"order_id": oid, "machine_id": loom, "shift_id": shift, "operator_id": op,  # noqa: E731
                                             "metres": 1}, headers=h))["qr"]
    opened = [ok(c.post(f"/qc/{roll()}", json={"result": "reject", "defect_type": "hole"}, headers=h))["defect_alerts"]
              for _ in range(5)]
    assert opened[:4] == [[], [], [], []] and {a["scope"] for a in opened[4]} == {"machine_shift", "operator"}
    assert "Loom D / D shift: 5 of 5" in opened[4][0]["suggestion"]
    assert ok(c.post(f"/qc/{roll()}", json={"result": "reject", "defect_type": "hole"}, headers=h))["defect_alerts"] == []  # updated, not reopened


def test_audit_fixes():
    """Bugs found in the audit: each line failed before its fix."""
    h = owner_headers()
    m = lambda t, d: ok(c.post(f"/master/{t}", json=d, headers=h))["id"]  # noqa: E731
    assert c.post("/master/buyers", json={}, headers=h).status_code == 422  # was a 500 (INSERT with no columns)

    # A person-edited PO line plus new demand used to crash every PO refresh (UNIQUE po_id, yarn_type_id).
    buyer, sup = m("buyers", {"name": "Audit"}), m("suppliers", {"name": "Audit Yarns"})
    yarn = m("yarn_types", {"count": "2s", "material": "coir", "colour": "brown"})
    fab = m("fabric_types", {"name": "Coir mat"})
    m("bom", {"fabric_type_id": fab, "yarn_type_id": yarn, "kg_per_m": 1})
    ok(c.post("/grn", json={"supplier_id": sup, "yarn_type_id": yarn, "qty_kg": 1, "rate": 10}, headers=h))
    order = {"buyer_id": buyer, "fabric_type_id": fab, "colour": "brown", "qty_m": 50, "deadline": "2099-01-01", "price_per_m": 1}
    oid = ok(c.post("/orders", json=order, headers=h))["id"]
    lines = lambda: [l for p in ok(c.get("/purchase-orders?status=draft", headers=h)) for l in p["lines"]  # noqa: E731
                     if l["yarn_type_id"] == yarn]
    ok(c.put(f"/po-lines/{lines()[0]['id']}", json={"qty_kg": 5}, headers=h))
    assert ok(c.post("/orders", json=order, headers=h))["po_refresh"] is not None
    assert sorted(l["qty_kg"] for l in lines()) == [5, 94]  # 100 needed - 1 in stock - 5 the person kept, on a new draft

    # Review kinds belong to their team.
    ok(c.post("/users", json={"username": "qc_audit", "password": "secret123", "role": "qc"}, headers=h))
    qc = {"Authorization": "Bearer " + ok(c.post("/auth/login", json={"username": "qc_audit", "password": "secret123"}))["token"]}
    real = ai.decide
    ai.decide = lambda state, qs: {n: {"choice": next(iter(o)), "probs": {}, "confidence": 0.1} for n, (_, o) in qs.items()}
    try:
        rid = ok(c.post("/messages", json={"text": "hi"}, headers=h))["routing"]["review_id"]
    finally:
        ai.decide = real
    assert all(r["kind"] != "message" for r in ok(c.get("/reviews?status=all", headers=qc)))
    assert c.post(f"/reviews/{rid}", json={"dept": "QC"}, headers=qc).status_code == 403

    # Jev answered the main question but gave nothing valid for urgency: urgency is left empty, not set to "low".
    real_post = ai._post
    ai._post = lambda path, body, timeout: {"answers": {"dept": {"choice": "Accounts", "confidence": 0.99},
                                                        "intent": {"choice": "payment", "confidence": 0.99}}}
    try:
        msg = ok(c.post("/messages", json={"text": "invoice?"}, headers=h))["message"]
    finally:
        ai._post = real_post
    assert msg["dept"] == "Accounts" and msg["urgency"] is None

    # Buyer payments go against an invoice and can't exceed what is due.
    loom, shift = m("machines", {"name": "Loom A", "kind": "weaving"}), m("shifts", {"name": "A shift"})
    op = m("workers", {"name": "A op", "wage_type": "daily", "rate": 1})
    qr = ok(c.post("/rolls", json={"order_id": oid, "machine_id": loom, "shift_id": shift, "operator_id": op, "metres": 10},
                   headers=h))["qr"]
    ok(c.post(f"/qc/{qr}", json={"result": "pass"}, headers=h))
    did = ok(c.post("/dispatches", json={"order_id": oid, "vehicle_no": "X", "driver": "Y"}, headers=h))["id"]
    ok(c.post(f"/dispatches/{did}/scan", json={"qr": qr}, headers=h))
    inv = ok(c.post(f"/dispatches/{did}/close", headers=h))["invoice"]
    pay = {"party": "buyer", "party_id": buyer, "amount": inv["total"]}
    assert c.post("/payments", json=pay, headers=h).status_code == 422
    assert c.post("/payments", json={**pay, "invoice_id": inv["id"], "amount": inv["total"] + 1}, headers=h).status_code == 422
    ok(c.post("/payments", json={**pay, "invoice_id": inv["id"]}, headers=h))

    # Business dates are local, not SQLite's UTC CURRENT_DATE.
    import datetime
    assert inv["issued_on"] == datetime.date.today().isoformat()


def test_voice_agent():
    """Retell tools: signed requests only, and a caller only hears about their own orders."""
    import hashlib, hmac, json as js, time as tm
    from mill import voice
    os.environ["RETELL_API_KEY"] = "test-key"
    def post(path, body, key="test-key"):
        raw, ts = js.dumps(body).encode(), str(int(tm.time() * 1000))
        sig = hmac.new(key.encode(), raw + ts.encode(), hashlib.sha256).hexdigest()
        return c.post(path, content=raw, headers={"x-retell-signature": f"v={ts},d={sig}", "content-type": "application/json"})
    h = owner_headers()
    m = lambda t, d: ok(c.post(f"/master/{t}", json=d, headers=h))["id"]  # noqa: E731
    acme = m("buyers", {"name": "Voice Acme Textiles", "phone": "+91 98765 43210"})
    m("buyers", {"name": "Someone Else"})
    fab = m("fabric_types", {"name": "Voice cloth"})
    oid = ok(c.post("/orders", json={"buyer_id": acme, "fabric_type_id": fab, "colour": "red", "qty_m": 100,
                                     "deadline": "2099-01-01", "price_per_m": 1}, headers=h))["id"]
    me, stranger = {"from_number": "+919876543210"}, {"from_number": "+919000000000"}

    assert post("/voice/retell/tools/order_status", {"args": {"order_number": oid}}, key="wrong").status_code == 401
    assert c.post("/voice/retell/tools/order_status", json={"args": {"order_number": oid}}).status_code == 401
    os.environ["VOICE_SECRET"] = "s" * 40  # the shared-secret route for keys without Retell's webhook badge
    assert c.post("/voice/retell/tools/identify_caller", json={"call": me}, headers={"x-voice-secret": "s" * 40}).status_code == 200
    assert c.post("/voice/retell/tools/identify_caller", json={"call": me}, headers={"x-voice-secret": "s" * 39}).status_code == 401
    del os.environ["VOICE_SECRET"]
    assert ok(post("/voice/retell/tools/identify_caller", {"call": me}))["company"] == "Voice Acme Textiles"
    assert ok(post("/voice/retell/tools/order_status", {"call": me, "args": {"order_number": f"#{oid}"}}))["ordered_metres"] == 100
    assert ok(post("/voice/retell/tools/order_status", {"call": stranger, "args": {"order_number": oid}}))["verified"] is False
    # web test calls have no number: the company name must match the order's buyer
    assert ok(post("/voice/retell/tools/order_status", {"call": {}, "args": {"order_number": oid, "company_name": "acme textiles"}}))["verified"]
    assert ok(post("/voice/retell/tools/order_status", {"call": {}, "args": {"order_number": oid, "company_name": "Someone Else"}}))["verified"] is False
    assert ok(post("/voice/retell/tools/invoices_due", {"call": me, "args": {"order_number": oid}}))["invoices"] == []
    # Browser calls opened from a tracking link: our server put the token in the call metadata.
    token = ok(c.get(f"/orders/{oid}", headers=h))["token"]
    linked = {"metadata": {"track_token": token}}
    who = ok(post("/voice/retell/tools/identify_caller", {"call": linked}))
    assert who["company"] == "Voice Acme Textiles" and who["calling_from_tracking_link_of_order"] == oid
    assert ok(post("/voice/retell/tools/order_status", {"call": linked, "args": {"order_number": oid}}))["verified"]
    other = ok(c.post("/orders", json={"buyer_id": m("buyers", {"name": "Third Co"}), "fabric_type_id": fab, "colour": "x",
                                       "qty_m": 1, "deadline": "2099-01-01", "price_per_m": 1}, headers=h))["id"]
    assert ok(post("/voice/retell/tools/order_status", {"call": linked, "args": {"order_number": other, "company_name": "Third Co"}}))["verified"] is False
    key = os.environ.pop("RETELL_API_KEY")
    assert c.post("/voice/web-call", json={"track": "nope"}).status_code == 503  # no Retell key: no calls, no network
    os.environ["RETELL_API_KEY"] = key

    # The finished call lands in the Inbox once, even if Retell retries the webhook.
    real = ai.decide
    ai.decide = lambda state, qs: {n: {"choice": next(iter(o)), "probs": {}, "confidence": 1.0} for n, (_, o) in qs.items()}
    try:
        call = {**me, "call_id": "call_123", "transcript": "Agent: Hello\nUser: where is my order", "call_analysis": {"call_summary": "Asked about order status."}}
        for _ in range(2):
            ok(post("/voice/retell/webhook", {"event": "call_analyzed", "call": call}))
    finally:
        ai.decide = real
    logged = [x for x in ok(c.get("/messages", headers=h)) if x["channel"] == "phone" and x["buyer_id"] == acme]
    assert len(logged) == 1 and logged[0]["text"].startswith("Phone call: Asked about order status.")


def test_scanned_roll_links():
    """A scanner gun types the label's whole link; trace, QC and truck loading all take it."""
    h = owner_headers()
    m = lambda t, d: ok(c.post(f"/master/{t}", json=d, headers=h))["id"]  # noqa: E731
    buyer, fab = m("buyers", {"name": "Scan Co"}), m("fabric_types", {"name": "Scan cloth"})
    loom, shift = m("machines", {"name": "Loom S", "kind": "weaving"}), m("shifts", {"name": "S shift"})
    op = m("workers", {"name": "S op", "wage_type": "daily", "rate": 1})
    oid = ok(c.post("/orders", json={"buyer_id": buyer, "fabric_type_id": fab, "colour": "x", "qty_m": 5,
                                     "deadline": "2099-01-01", "price_per_m": 1}, headers=h))["id"]
    qr = ok(c.post("/rolls", json={"order_id": oid, "machine_id": loom, "shift_id": shift, "operator_id": op, "metres": 5}, headers=h))["qr"]
    link = f"http://mill.example/#roll={qr}"
    from urllib.parse import quote
    assert ok(c.get("/rolls/" + quote(link, safe=""), headers=h))["qr"] == qr
    ok(c.post("/qc/" + quote(link, safe=""), json={"result": "pass"}, headers=h))
    did = ok(c.post("/dispatches", json={"order_id": oid, "vehicle_no": "S1", "driver": "d"}, headers=h))["id"]
    assert ok(c.post(f"/dispatches/{did}/scan", json={"qr": f"  {link}\n"}, headers=h))["rolls"] == 1


def test_attendance_register():
    """Past attendance can be read back by month; days present match what wages count."""
    h = owner_headers()
    m = lambda t, d: ok(c.post(f"/master/{t}", json=d, headers=h))["id"]  # noqa: E731
    w1, w2 = m("workers", {"name": "Reg A", "wage_type": "daily", "rate": 500}), m("workers", {"name": "Reg B", "wage_type": "daily", "rate": 500})
    day, night = m("shifts", {"name": "Reg Day"}), m("shifts", {"name": "Reg Night"})
    ok(c.post("/attendance", json={"shift_id": day, "work_date": "2026-09-03", "worker_ids": [w1, w2]}, headers=h))
    ok(c.post("/attendance", json={"shift_id": night, "work_date": "2026-09-03", "worker_ids": [w1]}, headers=h))  # double shift
    ok(c.post("/attendance", json={"shift_id": day, "work_date": "2026-09-04", "worker_ids": [w1]}, headers=h))
    ok(c.post("/attendance", json={"shift_id": day, "work_date": "2026-10-01", "worker_ids": [w1]}, headers=h))  # next month
    reg = ok(c.get("/attendance?month=2026-09", headers=h))
    mine = [(r["work_date"], r["shift"]) for r in reg["records"] if r["worker_id"] == w1]
    assert reg["month"] == "2026-09" and mine == [("2026-09-03", "Reg Day"), ("2026-09-03", "Reg Night"), ("2026-09-04", "Reg Day")]
    wage = {w["id"]: w for w in ok(c.get("/wages?month=2026-09", headers=h))["workers"]}
    assert wage[w1]["days"] == len({d for d, _ in mine}) == 2 and wage[w2]["days"] == 1
    ok(c.post("/users", json={"username": "qc_reg", "password": "secret123", "role": "qc"}, headers=h))
    qc = {"Authorization": "Bearer " + ok(c.post("/auth/login", json={"username": "qc_reg", "password": "secret123"}))["token"]}
    assert c.get("/attendance", headers=qc).status_code == 403
