"""The business rules in docs/test-plan.md (section B), each on its own fresh database with hand-worked numbers.
Run from backend/: python3 -m pytest -q tests"""
import json
import os
import tempfile
from datetime import date, timedelta

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "rules.db"))  # never mill.db, even run alone
os.environ.update(OPENROUTER_API_KEY="", SIM_SPEED="1", AI_THRESHOLD="0.8")  # same pins as test_app.py

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
from mill import ai, auth, db as mdb, digest, livefloor  # noqa: E402

c = TestClient(app.app)
TODAY = date.today()
H = {}  # role -> auth header, filled per test


@pytest.fixture(autouse=True)
def fresh_db():
    """Each test gets an empty database: the numbers below only add up if nothing else is in it."""
    old = mdb.DB_PATH
    mdb.DB_PATH = os.path.join(tempfile.mkdtemp(), "rules.db")
    mdb.init()
    auth.jwt_secret.cache_clear()
    auth.FAILED.clear()
    H["owner"] = bearer(ok(c.post("/auth/setup", json={"username": "boss", "password": "secret123", "email": "boss@mill.example", "phone": "+91 98400 11111"}))["token"])
    for r in ("admin", "store", "supervisor", "qc"):
        ok(c.post("/users", json={"username": r, "password": "secret123", "role": r}, headers=H["owner"]))
        H[r] = bearer(ok(c.post("/auth/login", json={"username": r, "password": "secret123"}))["token"])
    yield
    mdb.DB_PATH = old
    auth.jwt_secret.cache_clear()


def bearer(t):
    return {"Authorization": "Bearer " + t}


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def post(path, body=None, who="owner"):
    return ok(c.post(path, json=body, headers=H[who]))


def get(path, who="owner"):
    return ok(c.get(path, headers=H[who]))


def status(method, path, body=None, who="owner"):
    return c.request(method, path, json=body, headers=H[who]).status_code


def m(table, **row):
    return post(f"/master/{table}", row)["id"]


def order(buyer, fabric, qty, price=100, deadline="2099-01-01"):
    return post("/orders", {"buyer_id": buyer, "fabric_type_id": fabric, "colour": "x", "qty_m": qty,
                            "deadline": str(deadline), "price_per_m": price})["id"]


def stock(yarn):
    return next(y for y in get("/stock") if y["id"] == yarn)


def basics():
    """buyer, two suppliers, a loom, a shift and a daily operator."""
    return dict(buyer=m("buyers", name="B"), s1=m("suppliers", name="S1"), s2=m("suppliers", name="S2"),
                loom=m("machines", name="Loom X", kind="weaving", overhead_per_hour=100),
                shift=m("shifts", name="Day"), op=m("workers", name="Op", wage_type="daily", rate=1))


def roll(w, oid, metres, machine=None, shift=None, op=None):
    return post("/rolls", {"order_id": oid, "machine_id": machine or w["loom"], "shift_id": shift or w["shift"],
                           "operator_id": op or w["op"], "metres": metres})["qr"]


def ship(oid, qrs):
    """One truck with these rolls, closed. Returns the invoice."""
    d = post("/dispatches", {"order_id": oid, "vehicle_no": "TN", "driver": "D"})["id"]
    for q in qrs:
        post(f"/dispatches/{d}/scan", {"qr": q})
    return post(f"/dispatches/{d}/close")["invoice"]


def jev(answers):
    """Stand-in for Jev: answers = {question: (choice, confidence)}."""
    return lambda path, body, timeout: {"answers": {k: {"choice": ch, "confidence": cf} for k, (ch, cf) in answers.items()}}


# B1 ----------------------------------------------------------------------------------------------------------------
def test_stock_and_moving_average():
    w = basics()
    y = m("yarn_types", count="30s", material="cotton", colour="n", reorder_kg=950)
    f = m("fabric_types", name="F1")
    oid = order(w["buyer"], f, 10)
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y, "qty_kg": 500, "rate": 200})
    post("/grn", {"supplier_id": w["s2"], "yarn_type_id": y, "qty_kg": 500, "rate": 220})
    assert (stock(y)["stock_kg"], stock(y)["avg_rate"]) == (1000, 210)
    assert get("/alerts/low-stock") == []                                   # 1000 > reorder 950
    issue = post("/issues", {"order_id": oid, "yarn_type_id": y, "qty_kg": 100})
    assert issue["issue"]["rate"] == 210 and [x["id"] for x in issue["low_stock"]] == [y]  # 900 <= 950
    assert (stock(y)["stock_kg"], stock(y)["avg_rate"]) == (900, 210)
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y, "qty_kg": 100, "rate": 150})
    assert (stock(y)["stock_kg"], stock(y)["avg_rate"]) == (1000, 204)       # (189,000 + 15,000) / 1,000
    r = c.post("/issues", json={"order_id": oid, "yarn_type_id": y, "qty_kg": 1000.01}, headers=H["owner"])
    assert r.status_code == 409 and "only 1000 kg in stock" in r.json()["detail"]
    assert get("/issues?order_id=%d" % oid)[0]["qty_kg"] == 100
    # Writing yarn off (damaged, short on a count): stock goes down at the moving average, never below zero.
    wo = post("/stock/writeoffs", {"yarn_type_id": y, "qty_kg": 50, "reason": "damaged", "note": "rain leak"})
    assert wo["writeoff"]["rate"] == 204 and (stock(y)["stock_kg"], stock(y)["avg_rate"]) == (950, 204)
    r = c.post("/stock/writeoffs", json={"yarn_type_id": y, "qty_kg": 951, "reason": "wastage"}, headers=H["owner"])
    assert r.status_code == 409 and "only 950 kg in stock" in r.json()["detail"]
    assert c.post("/stock/writeoffs", json={"yarn_type_id": y, "qty_kg": -5, "reason": "wastage"}, headers=H["owner"]).status_code == 422
    assert get("/stock/writeoffs")[0]["note"] == "rain leak"


# B2 ----------------------------------------------------------------------------------------------------------------
def test_feasibility_first_come_first_served():
    w = basics()
    y1 = m("yarn_types", count="30s", material="cotton", colour="n")
    y2 = m("yarn_types", count="24s", material="poly", colour="navy")
    f2 = m("fabric_types", name="F2")
    m("bom", fabric_type_id=f2, yarn_type_id=y1, kg_per_m=0.1)
    m("bom", fabric_type_id=f2, yarn_type_id=y2, kg_per_m=0.3)
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y1, "qty_kg": 1000, "rate": 1})
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y2, "qty_kg": 100, "rate": 1})
    a, b = order(w["buyer"], f2, 200), order(w["buyer"], f2, 200)
    fa, fb = get(f"/orders/{a}/feasibility"), get(f"/orders/{b}/feasibility")
    assert (fa["can_start"], fa["short_kg"]) == (True, 0)
    line = next(x for x in fb["lines"] if x["yarn_type_id"] == y2)
    assert (line["required_kg"], line["reserved_kg"], line["free_kg"], line["short_kg"]) == (60, 60, 40, 20)
    assert fb["verdict"] == "short by 20 kg"
    post("/issues", {"order_id": a, "yarn_type_id": y2, "qty_kg": 60})       # moving yarn creates none
    line = next(x for x in get(f"/orders/{b}/feasibility")["lines"] if x["yarn_type_id"] == y2)
    assert (line["available_kg"], line["reserved_kg"], line["short_kg"]) == (40, 0, 20)
    assert get(f"/orders/{a}/feasibility")["can_start"]
    nobom = order(w["buyer"], m("fabric_types", name="No recipe"), 10)
    assert get(f"/orders/{nobom}/feasibility") == {"can_start": False, "short_kg": 0, "verdict": "no BOM for this fabric type", "lines": []}


# B3 ----------------------------------------------------------------------------------------------------------------
def test_purchase_order_drafts():
    w = basics()
    y2 = m("yarn_types", count="24s", material="poly", colour="navy")
    f2 = m("fabric_types", name="F2")
    m("bom", fabric_type_id=f2, yarn_type_id=y2, kg_per_m=0.3)
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y2, "qty_kg": 100, "rate": 300})
    order(w["buyer"], f2, 200)
    order(w["buyer"], f2, 200)
    drafts = lambda: [(p["supplier_id"], l["qty_kg"], l["rate"], l["locked"])  # noqa: E731
                      for p in get("/purchase-orders?status=draft") for l in p["lines"]]
    assert drafts() == [(w["s1"], 20, 300, False)]                            # 120 demand - 100 stock
    po = get("/purchase-orders?status=draft")[0]
    assert "open orders still need 120 kg, stock is 100 kg" in po["lines"][0]["reason"]

    post("/grn", {"supplier_id": w["s2"], "yarn_type_id": y2, "qty_kg": 10, "rate": 280})
    assert drafts() == [(w["s2"], 10, 280, False)]                            # cheaper supplier, 10 kg still needed
    line = get("/purchase-orders?status=draft")[0]["lines"][0]
    c.put(f"/po-lines/{line['id']}", json={"qty_kg": 4}, headers=H["owner"])  # a person keeps 4 kg on this line
    assert sorted(drafts()) == sorted([(w["s2"], 4, 280, True), (w["s2"], 6, 280, False)])

    po = next(p for p in get("/purchase-orders?status=draft") if p["lines"][0]["locked"])
    assert status("POST", f"/purchase-orders/{po['id']}/approve", who="store") == 403
    post(f"/purchase-orders/{po['id']}/approve")
    assert status("POST", f"/purchase-orders/{po['id']}/approve") == 409
    assert status("PUT", f"/po-lines/{po['lines'][0]['id']}", {"qty_kg": 9}) == 409
    grn = {"supplier_id": w["s2"], "yarn_type_id": y2, "qty_kg": 4, "rate": 280, "po_line_id": po["lines"][0]["id"]}
    draft_line = next(l for p in get("/purchase-orders?status=draft") for l in p["lines"])
    assert status("POST", "/grn", {**grn, "po_line_id": draft_line["id"]}) == 409   # not approved
    assert status("POST", "/grn", {**grn, "supplier_id": w["s1"]}) == 409           # wrong supplier
    post("/grn", grn)
    assert get(f"/purchase-orders/{po['id']}")["status"] == "received"
    assert status("POST", f"/purchase-orders/{po['id']}/cancel") == 409             # yarn arrived

    # An approved PO that is cancelled before delivery: its kg are needed again, so they are drafted again.
    open_line = next(l for p in get("/purchase-orders?status=draft") for l in p["lines"])
    post(f"/purchase-orders/{open_line['po_id']}/approve")
    assert drafts() == []                                                     # everything is on order
    post(f"/purchase-orders/{open_line['po_id']}/cancel")
    assert drafts() == [(w["s2"], 6, 280, False)]


# B4, B5, B13 -------------------------------------------------------------------------------------------------------
def test_order_stage_expected_date_and_buyer_link():
    w = basics()
    dye = m("machines", name="Dye 1", kind="dyeing")
    f = m("fabric_types", name="F")
    oid = order(w["buyer"], f, 100, price=999)
    stage = lambda: get(f"/orders/{oid}")["stage"]  # noqa: E731
    assert stage() == "Pending"
    post("/jobs", {"order_id": oid, "machine_id": w["loom"]})
    assert stage() == "Weaving"
    post("/jobs", {"order_id": oid, "machine_id": dye})
    assert stage() == "Dyeing"
    bad = roll(w, oid, 60)
    assert stage() == "In QC"
    post(f"/qc/{bad}", {"result": "reject", "defect_type": "hole"})
    assert stage() == "Dyeing"                                                # rejected metres count nowhere
    r1, r2 = roll(w, oid, 60), roll(w, oid, 40)
    post(f"/qc/{r1}", {"result": "pass"})
    assert stage() == "In QC"
    post(f"/qc/{r2}", {"result": "pass"})
    assert stage() == "Ready"                                                 # passed 100 of 100
    ship(oid, [r1])
    assert stage() == "Ready"                                                 # 60 of 100 shipped
    ship(oid, [r2])
    assert stage() == "Dispatched"

    # Expected ready: 30 m + 10 m on two days this week = 20 m/day; 60 m left -> 3 days.
    o2 = order(w["buyer"], f, 100)
    assert get(f"/orders/{o2}")["expected_ready"] is None
    for d, out in ((2, 30), (1, 10)):
        post("/production", {"entry_date": str(TODAY - timedelta(days=d)), "shift_id": w["shift"], "machine_id": w["loom"],
                             "order_id": o2, "operator_id": w["op"], "output": out, "hours": 1})
    o = get(f"/orders/{o2}")
    assert (o["produced_m"], o["progress_pct"], o["expected_ready"]) == (40, 40.0, str(TODAY + timedelta(days=3)))
    risk = get(f"/orders/{o2}/risk")
    assert (risk["at_risk"], risk["probability"], risk["progress_pct"]) == (None, None, 40.0)   # AI off: "unknown"

    # The buyer link: this order only, no prices or buyer details.
    token = get(f"/orders/{oid}")["token"]
    t = ok(c.get(f"/track/{token}"))
    assert t["order_no"] == oid and t["stage"] == "Dispatched" and len(t["dispatches"]) == 2
    text = json.dumps(t)
    assert "999" not in text and "price" not in text and '"B"' not in text and "token" not in text
    assert c.get("/track/not-a-token").status_code == 404


# B6 ----------------------------------------------------------------------------------------------------------------
def test_demo_clock_does_not_fast_forward_downtime():
    w = basics()
    oid = order(w["buyer"], m("fabric_types", name="F"), 10)
    post("/jobs", {"order_id": oid, "machine_id": w["loom"], "operator_id": w["op"], "shift_id": w["shift"]})
    db = mdb.connect()
    set_tick = lambda: db.execute("UPDATE job_cards SET last_tick = datetime('now', '-5 hours')")  # noqa: E731
    tick = lambda: db.execute("SELECT (julianday('now') - julianday(last_tick)) * 24 FROM job_cards").fetchone()[0]  # noqa: E731
    try:
        set_tick()
        livefloor.resume_demo_clock()                                         # real speed: nothing to do
        assert round(tick()) == 5
        livefloor.SIM_SPEED = 60
        livefloor.resume_demo_clock()
        assert tick() < 0.01                                                  # carries on from now
    finally:
        livefloor.SIM_SPEED = 1
        db.close()


# B7 ----------------------------------------------------------------------------------------------------------------
def test_defect_hotspot_thresholds():
    w = basics()
    rest = dict(machine=m("machines", name="Loom R", kind="weaving"), op=m("workers", name="Op R", wage_type="daily", rate=1))
    oid = order(w["buyer"], m("fabric_types", name="F"), 1000)
    qc = lambda q, res, dt=None: post(f"/qc/{q}", {"result": res, "defect_type": dt}, who="qc")["defect_alerts"]  # noqa: E731
    # Rest of the mill: 5 checks, 1 failure = 20%. So a hotspot needs max(2 x 20%, 20% + 15) = 40%.
    for res in ("reject", "pass", "pass", "pass", "pass"):
        assert qc(roll(w, oid, 1, **rest), res, "stain") == []                # 20% < max(2 x 10% assumed, 25%)
    results = [qc(roll(w, oid, 1), res, "hole") for res in ("reject", "reject", "pass", "pass", "pass")]
    assert results[:4] == [[], [], [], []]                                    # under 5 checks: noise
    assert sorted(a["scope"] for a in results[4]) == ["machine_shift", "operator"]   # 2 of 5 = 40%: alert
    a = next(x for x in results[4] if x["scope"] == "machine_shift")
    assert (a["checks"], a["defects"], a["rate"], a["baseline"], a["top_defect"]) == (5, 2, 0.4, 0.2, "hole")
    assert a["suggestion"].startswith("Loom X / Day: 2 of 5 QC checks failed (40%) against 20% elsewhere")
    assert "warp tension" in a["suggestion"]
    post(f"/alerts/defects/{a['id']}/ack", who="supervisor")
    assert [x["status"] for x in get("/alerts/defects") if x["id"] == a["id"]] == ["acked"]

    rep = get("/reports/defects?group=machine", who="qc")
    assert rep["mill"] == {"checks": 10, "defects": 3, "rate": 0.3}
    assert [(r["label"], r["checks"], r["defects"], r["rate"]) for r in rep["rows"]] == [("Loom X", 5, 2, 0.4), ("Loom R", 5, 1, 0.2)]
    by_type = get("/reports/defects?group=type", who="qc")["rows"]
    assert [(r["label"], r["defects"], r["share"]) for r in by_type] == [("hole", 2, 0.6667), ("stain", 1, 0.3333)]

    assert qc(roll(w, oid, 1), "pass") == []                                  # 2 of 6 = 33%: back to normal
    assert get("/alerts/defects") == []
    assert {x["status"] for x in get("/alerts/defects?status=cleared")} == {"cleared"}
    assert status("POST", f"/alerts/defects/{a['id']}/ack") == 409


# B8 ----------------------------------------------------------------------------------------------------------------
def test_dispatch_rules_and_invoice_rounding():
    w = basics()
    f = m("fabric_types", name="F", gst_rate=12)
    oid = order(w["buyer"], f, 151, price=333.33)
    qrs = [roll(w, oid, 75.5), roll(w, oid, 75.5)]
    for q in qrs:
        post(f"/qc/{q}", {"result": "pass"})
    d = post("/dispatches", {"order_id": oid, "vehicle_no": "TN", "driver": "D"})["id"]
    assert status("POST", f"/dispatches/{d}/close") == 409                    # nothing scanned
    for q in qrs:
        post(f"/dispatches/{d}/scan", {"qr": q})
    inv = post(f"/dispatches/{d}/close")["invoice"]
    assert (inv["metres"], inv["subtotal"], inv["gst"], inv["total"]) == (151, 50332.83, 6039.94, 56372.77)
    assert status("POST", f"/dispatches/{d}/close") == 409
    late = roll(w, oid, 1)
    post(f"/qc/{late}", {"result": "pass"})
    assert status("POST", f"/dispatches/{d}/scan", {"qr": late}) == 409      # truck closed
    assert status("POST", f"/dispatches/{d}/scan", {"qr": "ETM-NOPE"}) == 409  # closed is checked first
    d2 = post("/dispatches", {"order_id": oid, "vehicle_no": "TN", "driver": "D"})["id"]
    assert status("POST", f"/dispatches/{d2}/scan", {"qr": "ETM-NOPE"}) == 404
    assert "already scanned" in c.post(f"/dispatches/{d2}/scan", json={"qr": qrs[0]}, headers=H["owner"]).json()["detail"]
    detail = get(f"/dispatches/{d}")
    assert (detail["rolls"], detail["metres"], [p["qr"] for p in detail["packing_list"]]) == (2, 151, qrs)


# B9, B10 -----------------------------------------------------------------------------------------------------------
def test_money_month_end():
    w = basics()
    y = m("yarn_types", count="20s", material="cotton", colour="e")
    f = m("fabric_types", name="F", gst_rate=5)
    m("bom", fabric_type_id=f, yarn_type_id=y, kg_per_m=0.5)
    piece = m("workers", name="Piece", wage_type="piece", rate=2)
    daily = m("workers", name="Daily", wage_type="daily", rate=600)
    night = m("shifts", name="Night")
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y, "qty_kg": 100, "rate": 100})          # bill 10,000
    oid = order(w["buyer"], f, 100, price=300)
    post("/issues", {"order_id": oid, "yarn_type_id": y, "qty_kg": 50})                              # 5,000 of yarn
    for op, out, hrs in ((piece, 100, 5), (daily, 0, 3)):
        post("/production", {"shift_id": w["shift"], "machine_id": w["loom"], "order_id": oid, "operator_id": op,
                             "output": out, "hours": hrs})
    post("/attendance", {"shift_id": w["shift"], "worker_ids": [piece, daily]})
    post("/attendance", {"shift_id": night, "worker_ids": [daily]})                                  # double shift = 1 day
    post(f"/machines/{w['loom']}/status", {"status": "Breakdown"})

    wg = {x["id"]: x for x in get("/wages")["workers"]}
    assert (wg[piece]["wage"], wg[daily]["days"], wg[daily]["wage"], get("/wages")["total"]) == (200, 1, 600, 800)
    q = roll(w, oid, 100)
    post(f"/qc/{q}", {"result": "pass"})
    inv = ship(oid, [q])
    assert (inv["subtotal"], inv["gst"], inv["total"]) == (30000, 1500, 31500)
    # yarn 5,000 + labour (200 piece + 600 x 3h/3h daily) + overhead 8h x 100
    assert get(f"/reports/order-cost/{oid}") == {"order_id": oid, "yarn": 5000, "wages": 800, "overhead": 800,
                                                  "total_cost": 6600, "revenue": 30000, "margin": 23400,
                                                  "made_m": 100, "cost_per_m": 66}   # 6,600 / 100 m made
    p = get("/reports/pnl")
    assert (p["revenue"], p["yarn"], p["wages"], p["overhead"], p["total_costs"], p["profit"]) == (30000, 5000, 800, 800, 6600, 23400)
    assert get("/reports/pnl?month=2020-01")["total_costs"] == 0
    mach = next(x for x in get("/reports/machines") if x["id"] == w["loom"])
    assert (mach["output"], mach["hours"], mach["breakdowns"]) == (100, 8, 1)

    pay = {"party": "buyer", "party_id": w["buyer"], "invoice_id": inv["id"]}
    post("/payments", {**pay, "amount": 10000})
    assert status("POST", "/payments", {**pay, "amount": 21500.01}) == 422    # only 21,500 due
    other = m("buyers", name="Other")
    assert status("POST", "/payments", {**pay, "party_id": other, "amount": 1}) == 422
    assert status("POST", "/payments", {"party": "supplier", "party_id": w["s1"], "invoice_id": inv["id"], "amount": 1}) == 422
    rec = get("/reports/receivables")[0]
    assert (rec["total"], rec["paid"], rec["outstanding"]) == (31500, 10000, 21500)
    post("/payments", {"party": "supplier", "party_id": w["s1"], "amount": 4000})
    pb = next(x for x in get("/reports/payables") if x["id"] == w["s1"])
    assert (pb["billed"], pb["paid"], pb["outstanding"]) == (10000, 4000, 6000)


# B11 ---------------------------------------------------------------------------------------------------------------
def test_ai_confidence_gate_and_accuracy(monkeypatch):
    def message(dept, conf, intent="status"):
        monkeypatch.setattr(ai, "_post", jev({"dept": (dept, conf), "intent": (intent, 0.9), "urgency": ("high", 0.9)}))
        return post("/messages", {"text": "where is it?"})

    at = message("Production", 0.8)                                           # exactly the threshold: applied
    assert at["routing"]["auto"] and at["message"]["status"] == "routed" and at["message"]["dept"] == "Production"
    below = message("Production", 0.79)
    assert not below["routing"]["auto"] and below["message"]["status"] == "review" and below["message"]["dept"] is None
    other = message("Office", 0.99, intent="other")                            # can't be routed: a person decides
    assert not other["routing"]["auto"]
    junk = message("Marketing", 0.99)                                          # not an option: confidence 0
    assert not junk["routing"]["auto"] and junk["routing"]["confidence"] == 0

    monkeypatch.setattr(ai, "_post", jev({"category": ("electrical", 0.9), "severity": ("med", 0.9)}))
    loom = m("machines", name="L", kind="weaving")
    t = post(f"/machines/{loom}/status", {"status": "Breakdown", "note": "motor trips"})
    assert t["triage"]["auto"] and (t["log"]["category"], t["log"]["assignee"]) == ("electrical", "electrician")

    queue = {r["ref_id"]: r for r in get("/reviews") if r["kind"] == "message"}
    assert set(queue) == {below["message"]["id"], other["message"]["id"], junk["message"]["id"]}
    post(f"/reviews/{queue[below['message']['id']]['id']}", {"dept": "Dispatch"})   # a person disagrees
    post(f"/reviews/{queue[other['message']['id']]['id']}")                         # a person accepts
    assert get("/messages?status=routed")[0]["dept"] in ("Office", "Dispatch")
    assert status("POST", f"/reviews/{queue[junk['message']['id']]['id']}", {"dept": "Marketing"}) == 422
    assert status("POST", f"/reviews/{queue[junk['message']['id']]['id']}", {"colour": "red"}) == 422
    acc = get("/reviews/accuracy")
    assert acc["threshold"] == 0.8
    k = acc["kinds"]["message"]
    assert (k["decisions"], k["auto_applied"], k["human_checked"]) == (3, 1, 2)   # the confidence-0 one isn't a decision
    assert k["bands"]["0.7-0.8"] == {"checked": 1, "agreed": 0, "accuracy": 0.0}
    assert k["bands"]["0.9-1.0"] == {"checked": 1, "agreed": 1, "accuracy": 1.0}

    # A message already replied to stays "replied" when its routing is corrected later.
    mid = at["message"]["id"]
    post(f"/messages/{mid}/reply", {"text": "Ready Friday."})
    rid = next(r["id"] for r in get("/reviews?status=resolved") if r["kind"] == "message" and r["ref_id"] == mid)
    post(f"/reviews/{rid}", {"dept": "Office"})
    assert next(x for x in get("/messages") if x["id"] == mid)["status"] == "replied"


# B12 ---------------------------------------------------------------------------------------------------------------
def test_digest_facts_and_number_check(monkeypatch):
    w = basics()
    y = m("yarn_types", count="40s", material="cotton", colour="w", reorder_kg=50)
    f = m("fabric_types", name="F", gst_rate=0)
    m("bom", fabric_type_id=f, yarn_type_id=y, kg_per_m=0.1)
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y, "qty_kg": 10, "rate": 1})
    late = order(w["buyer"], f, 100, deadline=TODAY - timedelta(days=1))
    invs = []
    for _ in range(2):
        q = roll(w, late, 10)
        post(f"/qc/{q}", {"result": "pass"})
        invs.append(ship(late, [q]))                                          # 2 invoices of 1,000
    db = mdb.connect()
    db.execute("UPDATE invoices SET issued_on = ? WHERE id = ?", (str(TODAY - timedelta(days=30)), invs[0]["id"]))
    db.execute("UPDATE invoices SET issued_on = ? WHERE id = ?", (str(TODAY - timedelta(days=31)), invs[1]["id"]))
    db.close()
    post("/payments", {"party": "buyer", "party_id": w["buyer"], "invoice_id": invs[1]["id"], "amount": 250})

    monkeypatch.setattr(ai, "write_digest", lambda facts: "There are 99 problems.")   # 99 is not a fact
    d = post("/digest/refresh")
    facts = d["facts"]
    assert d["source"] == "template"
    assert facts["overdue_receivables"]["items"] == [{"invoice_id": invs[1]["id"], "buyer": "B", "outstanding": 750,
                                                      "days_overdue": 1}]     # 30 days is still within credit
    assert facts["low_stock"]["count"] == 1 and facts["low_stock"]["items"][0]["stock_kg"] == 10
    assert [o["order_id"] for o in facts["orders_at_risk"]["items"]] == [late]   # AI off: the rule stands
    assert "past its deadline" in facts["orders_at_risk"]["items"][0]["flags"]
    assert facts["machines_not_running"]["count"] == 1 and facts["po_drafts_waiting"]["count"] == 1
    assert "1 overdue invoice(s), ₹750 outstanding." in d["summary"]

    monkeypatch.setattr(ai, "write_digest", lambda facts: "One overdue invoice, 750 outstanding.")
    d = post("/digest/refresh")
    assert (d["source"], d["summary"]) == ("ai", "One overdue invoice, 750 outstanding.")
    assert get("/digest/today")["summary"] == d["summary"]                    # kept for the day


# B14 ---------------------------------------------------------------------------------------------------------------
# Who may call what, from spec section 2 and 3. "all" = any signed-in user. The owner may always.
OFFICE, STORE, FLOOR, QC = "admin", "store", "supervisor", "qc"
ALL = {OFFICE, STORE, FLOOR, QC}
MATRIX = [
    ("GET", "/me", ALL), ("GET", "/users", {OFFICE}), ("POST", "/users", {OFFICE}), ("DELETE", "/users/99999", {OFFICE}), ("PUT", "/users/99999", {OFFICE}),
    ("DELETE", "/workers/99999", {OFFICE}),
    ("GET", "/master/buyers", ALL), ("POST", "/master/buyers", {OFFICE}), ("PUT", "/master/buyers/99999", {OFFICE}),
    ("GET", "/orders", ALL), ("POST", "/orders", {OFFICE}), ("GET", "/orders/99999", ALL),
    ("GET", "/orders/99999/feasibility", ALL), ("GET", "/orders/99999/risk", ALL),
    ("GET", "/stock", ALL), ("GET", "/alerts/low-stock", ALL), ("GET", "/grn", ALL), ("POST", "/grn", {OFFICE, STORE}),
    ("POST", "/grn/challan", {OFFICE, STORE}), ("GET", "/issues", ALL), ("POST", "/issues", {OFFICE, STORE}),
    ("POST", "/stock/writeoffs", {OFFICE, STORE}), ("GET", "/stock/writeoffs", ALL),
    ("GET", "/purchase-orders", ALL), ("GET", "/purchase-orders/99999", ALL), ("POST", "/purchase-orders", {OFFICE}), ("POST", "/purchase-orders/refresh", {OFFICE, STORE}),
    ("PUT", "/po-lines/99999", {OFFICE}), ("POST", "/purchase-orders/99999/approve", {OFFICE}),
    ("POST", "/purchase-orders/99999/cancel", {OFFICE}),
    ("POST", "/jobs", {OFFICE, FLOOR}), ("POST", "/workers/99999/release", {OFFICE, FLOOR}), ("POST", "/jobs/99999/done", {OFFICE, FLOOR}),
    ("POST", "/machines/99999/status", {OFFICE, FLOOR}), ("GET", "/machines/99999/log", ALL),
    ("GET", "/production", ALL), ("POST", "/production", {OFFICE, FLOOR}), ("GET", "/floor", ALL),
    ("GET", "/rolls", ALL), ("POST", "/rolls", {OFFICE, FLOOR}), ("GET", "/rolls/ETM-X", ALL),
    ("GET", "/qc/queue", ALL), ("POST", "/qc/ETM-X", {OFFICE, QC}),
    ("GET", "/alerts/defects", ALL), ("POST", "/alerts/defects/99999/ack", {OFFICE, QC, FLOOR}),
    ("GET", "/reports/defects", {OFFICE, QC}),
    ("GET", "/finished-goods", ALL), ("GET", "/dispatches", ALL), ("GET", "/dispatches/99999", ALL),
    ("POST", "/dispatches", {OFFICE, STORE}), ("POST", "/dispatches/99999/scan", {OFFICE, STORE}),
    ("POST", "/dispatches/99999/close", {OFFICE, STORE}),
    ("GET", "/attendance", {OFFICE, FLOOR}), ("POST", "/attendance", {OFFICE, FLOOR}), ("GET", "/wages", {OFFICE}),
    ("POST", "/payments", {OFFICE}), ("GET", "/reports/receivables", {OFFICE}), ("GET", "/reports/payables", {OFFICE}),
    ("GET", "/reports/pnl", set()), ("GET", "/reports/order-cost", set()), ("GET", "/reports/order-cost/99999", set()),
    ("GET", "/reports/machines", set()),
    ("GET", "/messages", {OFFICE}), ("POST", "/messages", {OFFICE}), ("POST", "/messages/99999/draft", {OFFICE}),
    ("POST", "/messages/99999/reply", {OFFICE}),
    ("GET", "/reviews", {OFFICE, FLOOR, QC}), ("POST", "/reviews/99999", {OFFICE, FLOOR, QC}),
    ("GET", "/reviews/accuracy", {OFFICE}),
    ("GET", "/digest/today", {OFFICE}), ("POST", "/digest/refresh", {OFFICE}),
    ("POST", "/assistant/chat", ALL), ("GET", "/assistant/chat", ALL), ("GET", "/assistant/conversations", ALL),
]


def test_permission_matrix():
    wrong = []
    for method, path, allowed in MATRIX:
        assert c.request(method, path).status_code == 401, path              # signed out: nothing
        for who in ("owner", *ALL):
            code = status(method, path) if who == "owner" else status(method, path, who=who)
            if (code != 403) != (who == "owner" or who in allowed):
                wrong.append(f"{who} {method} {path} -> {code}")
    assert not wrong, wrong
    routes = {(m_, r.path) for r in app.app.routes for m_ in getattr(r, "methods", ()) if not r.path.startswith(("/voice", "/docs", "/redoc", "/openapi"))}
    covered = {(mt, p) for mt, p, _ in MATRIX}
    norm = lambda p: p.replace("99999", "{}").replace("ETM-X", "{}").replace("buyers", "{}")  # noqa: E731
    import re
    route_shapes = {(mt, re.sub(r"\{[^}]+\}", "{}", p)) for mt, p in routes}
    missing = route_shapes - {(mt, norm(p)) for mt, p in covered} - {("POST", "/auth/setup"), ("POST", "/auth/login"),
                                                                     ("POST", "/auth/logout"), ("GET", "/track/{}"), ("HEAD", "/docs"),
                                                                     ("POST", "/assistant/whatsapp")}  # bot secret, tested in test_assistant
    assert not missing, f"endpoints with no permission check here: {sorted(missing)}"
    # An office user can't make an owner.
    assert status("POST", "/users", {"username": "o2", "password": "secret123", "role": "owner"}, who=OFFICE) == 403
    # Accounts carry an email and phone; bad ones and taken usernames are refused.
    new = {"username": "ravi", "password": "secret123", "role": "store", "email": "ravi@mill.example", "phone": "+91 98400 33333"}
    assert status("POST", "/users", new, who=OFFICE) == 200
    assert status("POST", "/users", new, who=OFFICE) == 409
    assert status("POST", "/users", {**new, "username": "r2", "email": "not-an-email"}, who=OFFICE) == 422
    assert status("POST", "/users", {**new, "username": "r3", "phone": "call me"}, who=OFFICE) == 422
    assert {"email": "ravi@mill.example", "phone": "+91 98400 33333"}.items() <= next(
        x for x in c.get("/users", headers=H["owner"]).json() if x["username"] == "ravi").items()
    # Editing contact details: only the fields sent change; bad ones are refused.
    r = c.put(f"/users/{rid0}", json={"phone": "+91 90000 11111"}, headers=H[OFFICE]).json() if (rid0 := next(
        x["id"] for x in c.get("/users", headers=H["owner"]).json() if x["username"] == "ravi")) else None
    assert (r["email"], r["phone"]) == ("ravi@mill.example", "+91 90000 11111")
    assert c.put(f"/users/{rid0}", json={"email": "nope"}, headers=H[OFFICE]).status_code == 422
    # Removing an account: it can't sign in, its live session ends, it leaves the list; nobody removes themselves.
    rid = next(x["id"] for x in c.get("/users", headers=H["owner"]).json() if x["username"] == "ravi")
    tok = {"Authorization": "Bearer " + c.post("/auth/login", json={"username": "ravi", "password": "secret123"}).json()["token"]}
    assert status("DELETE", f"/users/{rid}", who=OFFICE) == 200
    assert c.get("/me", headers=tok).status_code == 401
    assert c.post("/auth/login", json={"username": "ravi", "password": "secret123"}).status_code == 401
    assert "ravi" not in [x["username"] for x in c.get("/users", headers=H["owner"]).json()]
    assert status("DELETE", f"/users/{c.get('/me', headers=H['owner']).json()['id']}") == 409


# B15 ---------------------------------------------------------------------------------------------------------------
def test_input_safety():
    w = basics()
    assert status("GET", "/master/users") == 404
    assert status("POST", "/master/buyers", {"name": "x", "is_admin": 1}) == 422
    assert status("PUT", f"/master/machines/{w['loom']}", {"status": "Running"}) == 422   # only via the status control
    assert status("PUT", "/master/buyers/99999", {"name": "x"}) == 404
    r = c.post("/attendance", json={"shift_id": w["shift"], "worker_ids": [99999]}, headers=H["owner"])
    assert r.status_code == 409 and r.json()["detail"] == "It refers to a record that does not exist."
    r = c.post("/orders", json={"buyer_id": w["buyer"], "fabric_type_id": 99999, "colour": "x", "qty_m": 1,
                                "deadline": "2099-01-01", "price_per_m": 1}, headers=H["owner"])
    assert r.status_code == 409
    r = c.post("/orders", json={"buyer_id": w["buyer"], "qty_m": -1}, headers=H["owner"])
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert status("POST", "/qc/ETM-NOPE", {"result": "pass"}) == 404
    assert status("POST", f"/machines/{w['loom']}/status", {"status": "Exploded"}) == 422


# The brief: "nobody knows the real cost of a single roll", and "nothing forgotten in a corner".
def test_cost_of_one_roll_and_rolls_waiting_on_the_shelf():
    w = basics()
    y = m("yarn_types", count="20s", material="cotton", colour="e")
    f = m("fabric_types", name="F")
    m("bom", fabric_type_id=f, yarn_type_id=y, kg_per_m=0.5)
    piece = m("workers", name="Piece", wage_type="piece", rate=2)
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y, "qty_kg": 100, "rate": 100})
    oid = order(w["buyer"], f, 100, price=300)
    post("/issues", {"order_id": oid, "yarn_type_id": y, "qty_kg": 40})                  # 4,000 of yarn
    post("/production", {"shift_id": w["shift"], "machine_id": w["loom"], "order_id": oid, "operator_id": piece,
                         "output": 80, "hours": 4})                                        # 160 wages + 400 overhead
    old, new = roll(w, oid, 50), roll(w, oid, 30)
    for q in (old, new):
        post(f"/qc/{q}", {"result": "pass"})
    # 4,560 for 80 m made = 57 per metre; a 50 m roll cost 2,850.
    assert get(f"/reports/order-cost/{oid}")["cost_per_m"] == 57
    assert get(f"/rolls/{old}")["cost"] == {"per_m": 57, "roll": 2850}
    assert get(f"/rolls/{old}", who="admin")["cost"]["roll"] == 2850
    assert get(f"/rolls/{old}", who="qc")["cost"] is None                               # money is for owner and office
    # Yarn issued ahead for metres not made yet doesn't load the metres made: 40 kg more issued, still 57 per metre.
    post("/issues", {"order_id": oid, "yarn_type_id": y, "qty_kg": 40})
    assert get(f"/reports/order-cost/{oid}")["cost_per_m"] == 57

    db = mdb.connect()
    db.execute("UPDATE rolls SET made_on = ? WHERE qr = ?", (str(TODAY - timedelta(days=12)), old))
    db.close()
    fg = get("/finished-goods")[0]
    assert (fg["rolls"], fg["days_on_shelf"], fg["waiting_long"], fg["long_rolls"]) == (2, 12, 1, old)
    d = post("/digest/refresh")
    assert d["facts"]["rolls_waiting_to_ship"] == {"days": 7, "count": 1}
