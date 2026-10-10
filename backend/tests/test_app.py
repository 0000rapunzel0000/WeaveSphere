"""Walks the demo script end to end. Run from backend/: python3 -m pytest -q tests"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.pop("OPENROUTER_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

import ai  # noqa: E402
import app  # noqa: E402

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


if __name__ == "__main__":
    test_demo_flow()
    print("ok")
