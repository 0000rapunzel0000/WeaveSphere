"""Everest Textile Mills backend. Run: uvicorn app:app --reload  (API docs at /docs)"""
import hashlib
import json
import math
import os
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import ai

DB_PATH = os.environ.get("DB_PATH", "mill.db")
app = FastAPI(title="Everest Textile Mills")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


def connect():
    db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


_db = connect()
_db.execute("PRAGMA journal_mode = WAL")
_db.executescript(open(os.path.join(os.path.dirname(__file__), "db", "schema.sql")).read())
_db.close()


def get_db():
    db = connect()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def tx(db):
    """Write lock for check-then-write sequences (stock, dispatch guards)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


@app.exception_handler(sqlite3.IntegrityError)
def integrity_error(_, e):
    return JSONResponse({"detail": str(e)}, status_code=409)


def rows(cur):
    return [dict(r) for r in cur]


def one(db, sql, *args):
    r = db.execute(sql, args).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    return dict(r)


def insert(db, table, data):
    """table and keys come from code or are checked against the schema, never raw user input."""
    return db.execute(f"INSERT INTO {table} ({', '.join(data)}) VALUES ({', '.join(':' + k for k in data)})",
                      data).lastrowid


def month_of(month: str | None):
    return month or date.today().strftime("%Y-%m")


# ---------- Auth ----------

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()


def user(authorization: str = Header(""), db=Depends(get_db)):
    u = db.execute("SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
                   "WHERE s.token = ?", (authorization.removeprefix("Bearer "),)).fetchone()
    if not u:
        raise HTTPException(401, "login required")
    return dict(u)


def role(*roles):
    """Owner may do anything; everyone else only what is listed."""
    def check(u=Depends(user)):
        if u["role"] not in ("owner", *roles):
            raise HTTPException(403, "not allowed for your role")
        return u
    return Depends(check)


Role = Literal["owner", "admin", "store", "supervisor", "qc"]


class UserIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    role: Role


class Login(BaseModel):
    username: str
    password: str


@app.post("/auth/setup")
def setup(body: Login, db=Depends(get_db)):
    """Creates the first owner. Only works on an empty database."""
    if len(body.password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")
    with tx(db):
        if db.execute("SELECT 1 FROM users").fetchone():
            raise HTTPException(403, "already set up")
        insert(db, "users", {"username": body.username, "pw_hash": hash_pw(body.password), "role": "owner"})
    return login(body, db)


@app.post("/auth/login")
def login(body: Login, db=Depends(get_db)):
    u = db.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    if not u or not secrets.compare_digest(hash_pw(body.password, u["pw_hash"].split("$")[0]), u["pw_hash"]):
        raise HTTPException(401, "wrong username or password")
    token = secrets.token_urlsafe(32)
    insert(db, "sessions", {"token": token, "user_id": u["id"]})
    return {"token": token, "role": u["role"], "username": u["username"]}


@app.post("/auth/logout")
def logout(authorization: str = Header(""), db=Depends(get_db)):
    db.execute("DELETE FROM sessions WHERE token = ?", (authorization.removeprefix("Bearer "),))
    return {"ok": True}


@app.get("/me")
def me(u=Depends(user)):
    return u


@app.post("/users")
def create_user(body: UserIn, db=Depends(get_db), u=role("admin")):
    if body.role == "owner" and u["role"] != "owner":
        raise HTTPException(403, "only an owner can create an owner")
    uid = insert(db, "users", {"username": body.username, "pw_hash": hash_pw(body.password), "role": body.role})
    return {"id": uid, "username": body.username, "role": body.role}


@app.get("/users")
def list_users(db=Depends(get_db), _=role("admin")):
    return rows(db.execute("SELECT id, username, role FROM users"))


# ---------- Master data ----------

MASTERS = {"buyers", "suppliers", "yarn_types", "fabric_types", "bom", "machines", "workers", "shifts"}


def master_cols(db, table, data):
    if table not in MASTERS:
        raise HTTPException(404, "unknown master table")
    cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")} - {"id"}
    if table == "machines":
        cols.discard("status")  # changed only via /machines/{id}/status so it gets logged
    if bad := set(data) - cols:
        raise HTTPException(422, f"unknown fields: {sorted(bad)}")


@app.get("/master/{table}")
def master_list(table: str, db=Depends(get_db), _=Depends(user)):
    master_cols(db, table, {})
    return rows(db.execute(f"SELECT * FROM {table} ORDER BY id"))


@app.post("/master/{table}")
def master_create(table: str, data: dict, db=Depends(get_db), _=role("admin")):
    master_cols(db, table, data)
    return one(db, f"SELECT * FROM {table} WHERE id = ?", insert(db, table, data))


@app.put("/master/{table}/{id}")
def master_update(table: str, id: int, data: dict, db=Depends(get_db), _=role("admin")):
    master_cols(db, table, data)
    if data:
        db.execute(f"UPDATE {table} SET {', '.join(k + ' = :' + k for k in data)} WHERE id = :_id", {**data, "_id": id})
    return one(db, f"SELECT * FROM {table} WHERE id = ?", id)


# ---------- Orders ----------

class OrderIn(BaseModel):
    buyer_id: int
    fabric_type_id: int
    colour: str
    qty_m: float = Field(gt=0)
    deadline: date
    price_per_m: float = Field(ge=0)


def roll_totals(db, oid):
    return dict(db.execute("""
        SELECT COUNT(*) rolls, COALESCE(SUM(metres), 0) produced_m,
          COUNT(*) FILTER (WHERE status IN ('passed', 'dispatched')) rolls_passed,
          COALESCE(SUM(metres) FILTER (WHERE status IN ('passed', 'dispatched')), 0) passed_m,
          COUNT(*) FILTER (WHERE status IN ('pending_qc', 'rework')) in_qc
        FROM rolls WHERE order_id = ? AND status != 'rejected'""", (oid,)).fetchone())


def order_status(db, o, t):
    if db.execute("SELECT 1 FROM dispatches WHERE order_id = ? AND status = 'closed'", (o["id"],)).fetchone():
        return "Dispatched"
    if t["passed_m"] >= o["qty_m"]:
        return "Ready"
    if t["in_qc"]:
        return "In QC"
    stage = db.execute("""SELECT m.kind FROM job_cards j JOIN machines m ON m.id = j.machine_id WHERE j.order_id = ?
        ORDER BY CASE m.kind WHEN 'dyeing' THEN 3 WHEN 'weaving' THEN 2 ELSE 1 END DESC LIMIT 1""",
                       (o["id"],)).fetchone()
    return stage["kind"].title() if stage else "Pending"


def feasibility(db, o):
    lines = rows(db.execute("""
        SELECT y.id yarn_type_id, y.count, y.material, y.colour, b.kg_per_m * :qty required_kg,
          COALESCE((SELECT SUM(qty_kg) FROM material_issues WHERE order_id = :oid AND yarn_type_id = y.id), 0) issued_kg,
          y.stock_kg available_kg
        FROM bom b JOIN yarn_stock y ON y.id = b.yarn_type_id WHERE b.fabric_type_id = :fid""",
                            {"qty": o["qty_m"], "oid": o["id"], "fid": o["fabric_type_id"]}))
    # ponytail: checks this order alone against free stock; doesn't reserve yarn for other open orders.
    for line in lines:
        line["short_kg"] = round(max(0, line["required_kg"] - line["issued_kg"] - line["available_kg"]), 2)
    short = round(sum(line["short_kg"] for line in lines), 2)
    if not lines:
        verdict = "no BOM for this fabric type"
    else:
        verdict = "can start" if short == 0 else f"short by {short:g} kg"
    return {"can_start": bool(lines) and short == 0, "short_kg": short, "verdict": verdict, "lines": lines}


def expected_ready(db, o, t):
    remaining = o["qty_m"] - t["passed_m"]
    if remaining <= 0:
        return date.today().isoformat()
    # ponytail: 7-day run rate of rolls made; swap for a per-stage plan if this proves too naive.
    rate = db.execute("SELECT COALESCE(SUM(metres), 0) / 7.0 FROM rolls WHERE order_id = ? AND status != 'rejected' "
                      "AND made_on >= date('now', '-7 day')", (o["id"],)).fetchone()[0]
    return (date.today() + timedelta(days=math.ceil(remaining / rate))).isoformat() if rate else None


def tracking(db, o):
    """What a buyer may see. Also the order context fed to Jev and the reply drafter."""
    t = roll_totals(db, o["id"])
    fabric = db.execute("SELECT name FROM fabric_types WHERE id = ?", (o["fabric_type_id"],)).fetchone()[0]
    return {
        "order_no": o["id"], "fabric": fabric, "colour": o["colour"], "stage": order_status(db, o, t),
        "target_m": o["qty_m"], "produced_m": t["produced_m"], "passed_m": t["passed_m"],
        "rolls_produced": t["rolls"], "rolls_passed": t["rolls_passed"],
        "progress_pct": round(100 * min(t["passed_m"] / o["qty_m"], 1), 1),
        "deadline": o["deadline"], "expected_ready": expected_ready(db, o, t),
        "dispatches": rows(db.execute("""
            SELECT d.id, d.vehicle_no, d.driver, d.status, d.created_at, COUNT(i.id) rolls, COALESCE(SUM(r.metres), 0) metres
            FROM dispatches d LEFT JOIN dispatch_items i ON i.dispatch_id = d.id LEFT JOIN rolls r ON r.id = i.roll_id
            WHERE d.order_id = ? GROUP BY d.id""", (o["id"],))),
    }


@app.post("/orders")
def create_order(body: OrderIn, db=Depends(get_db), _=role("admin")):
    oid = insert(db, "orders", {**body.model_dump(mode="json"), "token": secrets.token_urlsafe(16)})
    return order_detail(oid, db)


@app.get("/orders")
def list_orders(buyer_id: int | None = None, db=Depends(get_db), _=Depends(user)):
    orders = rows(db.execute("SELECT o.*, b.name buyer FROM orders o JOIN buyers b ON b.id = o.buyer_id "
                             "WHERE ? IS NULL OR o.buyer_id = ? ORDER BY o.id DESC", (buyer_id, buyer_id)))
    return [{**o, **tracking(db, o)} for o in orders]


@app.get("/orders/{oid}")
def order_detail(oid: int, db=Depends(get_db), _=Depends(user)):
    """Internal enquiry view: everything office staff need to answer a call."""
    o = one(db, "SELECT o.*, b.name buyer, b.phone buyer_phone FROM orders o JOIN buyers b ON b.id = o.buyer_id "
                "WHERE o.id = ?", oid)
    return {**o, **tracking(db, o), "tracking_path": f"/track/{o['token']}",
            "feasibility": feasibility(db, o),
            "job_cards": rows(db.execute("SELECT j.*, m.name machine, m.kind stage FROM job_cards j "
                                         "JOIN machines m ON m.id = j.machine_id WHERE j.order_id = ?", (oid,))),
            "invoices": rows(db.execute("SELECT * FROM invoices WHERE order_id = ?", (oid,)))}


@app.get("/orders/{oid}/feasibility")
def order_feasibility(oid: int, db=Depends(get_db), _=Depends(user)):
    return feasibility(db, one(db, "SELECT * FROM orders WHERE id = ?", oid))


@app.get("/orders/{oid}/risk")
def order_risk(oid: int, db=Depends(get_db), _=Depends(user)):
    o = one(db, "SELECT * FROM orders WHERE id = ?", oid)
    t = tracking(db, o)
    state = {"progress_pct": t["progress_pct"],
             "days_left": (date.fromisoformat(o["deadline"]) - date.today()).days,
             "yarn_short_kg": feasibility(db, o)["short_kg"],
             "free_machines": db.execute("SELECT COUNT(*) FROM machines WHERE status = 'Idle'").fetchone()[0]}
    r = ai.classify(state, ["yes", "no"])
    return {**state, "at_risk": r["choice"] == "yes", "probability": round(r["probs"]["yes"], 3),
            "confidence": round(r["confidence"], 3)}


@app.get("/track/{token}")
def track(token: str, db=Depends(get_db)):
    """Public buyer tracking link, no login."""
    return tracking(db, one(db, "SELECT * FROM orders WHERE token = ?", token))


# ---------- Raw material ----------

class GrnIn(BaseModel):
    supplier_id: int
    yarn_type_id: int
    qty_kg: float = Field(gt=0)
    rate: float = Field(ge=0)
    order_id: int | None = None
    challan_no: str | None = None


class IssueIn(BaseModel):
    order_id: int
    yarn_type_id: int
    qty_kg: float = Field(gt=0)
    machine_id: int | None = None


class ChallanIn(BaseModel):
    image_b64: str
    mime: str = "image/jpeg"


LOW_STOCK = "SELECT * FROM yarn_stock WHERE reorder_kg > 0 AND stock_kg <= reorder_kg"


@app.post("/grn")
def create_grn(body: GrnIn, db=Depends(get_db), u=role("admin", "store")):
    gid = insert(db, "grn", {**body.model_dump(), "created_by": u["id"]})
    return {"grn": one(db, "SELECT * FROM grn WHERE id = ?", gid),
            "stock": one(db, "SELECT * FROM yarn_stock WHERE id = ?", body.yarn_type_id),
            "low_stock": rows(db.execute(LOW_STOCK))}


@app.get("/grn")
def list_grn(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT g.*, s.name supplier FROM grn g JOIN suppliers s ON s.id = g.supplier_id "
                           "ORDER BY g.id DESC"))


@app.post("/grn/challan")
def grn_from_challan(body: ChallanIn, db=Depends(get_db), _=role("admin", "store")):
    """Photo of challan -> suggested GRN fields. Store keeper confirms by POSTing /grn."""
    try:
        f = ai.read_challan(body.image_b64, body.mime)
    except Exception as e:
        raise HTTPException(503, f"challan reader unavailable: {e}")
    supplier = db.execute("SELECT id FROM suppliers WHERE name LIKE ?", (f.get("supplier") or "",)).fetchone()
    yarn = db.execute("SELECT id FROM yarn_types WHERE count LIKE ? AND material LIKE ? AND colour LIKE ?",
                      (f.get("yarn_count") or "", f.get("material") or "", f.get("colour") or "")).fetchone()
    return {"extracted": f, "suggested": {"supplier_id": supplier and supplier[0], "yarn_type_id": yarn and yarn[0],
                                          "qty_kg": f.get("qty_kg"), "rate": f.get("rate"),
                                          "challan_no": f.get("challan_no")}}


@app.get("/stock")
def stock(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT *, reorder_kg > 0 AND stock_kg <= reorder_kg low FROM yarn_stock"))


@app.get("/alerts/low-stock")
def low_stock(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute(LOW_STOCK))


@app.post("/issues")
def issue_yarn(body: IssueIn, db=Depends(get_db), u=role("admin", "store")):
    with tx(db):
        have = one(db, "SELECT stock_kg FROM yarn_stock WHERE id = ?", body.yarn_type_id)["stock_kg"]
        if body.qty_kg > have:
            raise HTTPException(409, f"only {have:g} kg in stock")
        iid = insert(db, "material_issues", {**body.model_dump(), "created_by": u["id"]})
    return {"issue": one(db, "SELECT * FROM material_issues WHERE id = ?", iid),
            "low_stock": rows(db.execute(LOW_STOCK))}


@app.get("/issues")
def list_issues(order_id: int | None = None, db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT * FROM material_issues WHERE ? IS NULL OR order_id = ? ORDER BY id DESC",
                           (order_id, order_id)))


# ---------- AI decisions + human review queue ----------

LEVELS = ["low", "med", "high"]
HEADS = {  # kind -> (table, {field: options})
    "message": ("buyer_messages", {
        "intent": ["status", "quality_complaint", "payment", "new_order", "delivery_change", "other"],
        "dept": ["Office", "QC", "Production", "Dispatch", "Accounts"],
        "urgency": LEVELS}),
    "breakdown": ("machine_status_log", {
        "category": ["mechanical", "electrical", "yarn_quality", "operator"],
        "severity": LEVELS,
        "assignee": ["fitter", "electrician", "store", "supervisor"]}),
    "defect": ("qc_records", {
        "defect_type": ["hole", "colour_mismatch", "uneven_weave", "stain", "other"],
        "severity": LEVELS,
        "stage_at_fault": ["spinning", "weaving", "dyeing"]}),
}


def apply_decision(db, kind, ref_id, fields):
    table = HEADS[kind][0]
    sets = [f"{k} = ?" for k in fields] + (["status = 'routed'"] if kind == "message" else [])
    db.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", (*fields.values(), ref_id))


def decide(db, kind, ref_id, state, skip=()):
    """Confidence gate: auto-apply if every head clears the threshold, else queue for a human."""
    heads = {f: opts for f, opts in HEADS[kind][1].items() if f not in skip}
    with ThreadPoolExecutor() as ex:
        res = dict(zip(heads, ex.map(lambda f: ai.classify({**state, "decide": f}, heads[f]), heads)))
    choice = {f: r["choice"] for f, r in res.items()}
    conf = min(r["confidence"] for r in res.values())
    auto = conf >= ai.THRESHOLD
    if auto:
        apply_decision(db, kind, ref_id, choice)
    else:
        insert(db, "ai_reviews", {"kind": kind, "ref_id": ref_id, "suggestion": json.dumps(choice), "confidence": conf})
    return {"auto": auto, "confidence": round(conf, 3), "suggestion": choice,
            "probs": {f: r["probs"] for f, r in res.items()}}


@app.get("/reviews")
def review_queue(db=Depends(get_db), _=role("admin", "supervisor", "qc")):
    return [{**r, "suggestion": json.loads(r["suggestion"])}
            for r in rows(db.execute("SELECT * FROM ai_reviews WHERE status = 'open' ORDER BY id"))]


@app.post("/reviews/{rid}")
def resolve_review(rid: int, fields: dict[str, str], db=Depends(get_db), _=role("admin", "supervisor", "qc")):
    r = one(db, "SELECT * FROM ai_reviews WHERE id = ? AND status = 'open'", rid)
    allowed = {f: HEADS[r["kind"]][1][f] for f in json.loads(r["suggestion"])}
    for f, v in fields.items():
        if v not in allowed.get(f, ()):
            raise HTTPException(422, f"{f} must be one of {allowed.get(f)}")
    with tx(db):
        apply_decision(db, r["kind"], r["ref_id"], fields)
        db.execute("UPDATE ai_reviews SET status = 'resolved' WHERE id = ?", (rid,))
    return {"ok": True}


# ---------- Production floor ----------

class JobIn(BaseModel):
    order_id: int
    machine_id: int


class StatusIn(BaseModel):
    status: Literal["Running", "Idle", "Breakdown", "Maintenance"]
    note: str | None = None


class ProductionIn(BaseModel):
    entry_date: date = Field(default_factory=date.today)
    shift_id: int
    machine_id: int
    order_id: int
    operator_id: int
    output: float = Field(ge=0)
    hours: float = Field(0, ge=0)


class RollIn(BaseModel):
    order_id: int
    machine_id: int
    shift_id: int
    operator_id: int
    metres: float = Field(gt=0)


@app.post("/jobs")
def assign_job(body: JobIn, db=Depends(get_db), _=role("admin", "supervisor")):
    with tx(db):  # one open job per machine
        db.execute("UPDATE job_cards SET status = 'done' WHERE machine_id = ? AND status = 'open'", (body.machine_id,))
        jid = insert(db, "job_cards", body.model_dump())
    return one(db, "SELECT * FROM job_cards WHERE id = ?", jid)


@app.post("/jobs/{jid}/done")
def finish_job(jid: int, db=Depends(get_db), _=role("admin", "supervisor")):
    db.execute("UPDATE job_cards SET status = 'done' WHERE id = ?", (jid,))
    return one(db, "SELECT * FROM job_cards WHERE id = ?", jid)


@app.post("/machines/{mid}/status")
def machine_status(mid: int, body: StatusIn, db=Depends(get_db), _=role("admin", "supervisor")):
    m = one(db, "SELECT * FROM machines WHERE id = ?", mid)
    with tx(db):
        db.execute("UPDATE machines SET status = ? WHERE id = ?", (body.status, mid))
        lid = insert(db, "machine_status_log", {"machine_id": mid, **body.model_dump()})
    triage = None
    if body.status == "Breakdown" and body.note:
        recent = db.execute("SELECT COUNT(*) FROM machine_status_log WHERE machine_id = ? AND status = 'Breakdown' "
                            "AND at >= datetime('now', '-30 day')", (mid,)).fetchone()[0]
        triage = decide(db, "breakdown", lid, {"note": body.note, "machine": m["name"], "kind": m["kind"],
                                               "breakdowns_last_30_days": recent})
    return {"log": one(db, "SELECT * FROM machine_status_log WHERE id = ?", lid), "triage": triage}


@app.get("/machines/{mid}/log")
def machine_log(mid: int, db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT * FROM machine_status_log WHERE machine_id = ? ORDER BY id DESC", (mid,)))


@app.post("/production")
def log_production(body: ProductionIn, db=Depends(get_db), _=role("admin", "supervisor")):
    return one(db, "SELECT * FROM production_entries WHERE id = ?",
               insert(db, "production_entries", body.model_dump(mode="json")))


@app.get("/floor")
def floor(db=Depends(get_db), _=Depends(user)):
    machines = rows(db.execute("""
        SELECT m.*, j.order_id,
          COALESCE((SELECT SUM(output) FROM production_entries WHERE machine_id = m.id AND entry_date = date('now')), 0) today_output
        FROM machines m LEFT JOIN job_cards j ON j.machine_id = m.id AND j.status = 'open' ORDER BY m.kind, m.name"""))
    orders = {}
    for m in machines:
        m["idle"] = m["status"] != "Running"
        if m["order_id"] and m["order_id"] not in orders:
            orders[m["order_id"]] = tracking(db, one(db, "SELECT * FROM orders WHERE id = ?", m["order_id"]))
        m["order_progress_pct"] = orders[m["order_id"]]["progress_pct"] if m["order_id"] else None
    return {"machines": machines, "idle_count": sum(m["idle"] for m in machines), "orders": list(orders.values())}


@app.post("/rolls")
def create_roll(body: RollIn, db=Depends(get_db), _=role("admin", "supervisor")):
    """The QR code encodes `qr`; the client renders it as an image."""
    rid = insert(db, "rolls", {**body.model_dump(), "qr": "ETM-" + secrets.token_hex(5).upper()})
    return one(db, "SELECT * FROM rolls WHERE id = ?", rid)


@app.get("/rolls/{qr}")
def trace_roll(qr: str, db=Depends(get_db), _=Depends(user)):
    """Traceability: roll -> order, machine, shift, operator, yarn issued, QC history, dispatch."""
    r = one(db, """SELECT r.*, m.name machine, m.kind stage, s.name shift, w.name operator, b.name buyer
        FROM rolls r JOIN machines m ON m.id = r.machine_id JOIN shifts s ON s.id = r.shift_id
        JOIN workers w ON w.id = r.operator_id JOIN orders o ON o.id = r.order_id JOIN buyers b ON b.id = o.buyer_id
        WHERE r.qr = ?""", qr)
    return {**r,
            "order": tracking(db, one(db, "SELECT * FROM orders WHERE id = ?", r["order_id"])),
            "yarn_issued": rows(db.execute("""SELECT i.*, y.count, y.material, y.colour FROM material_issues i
                JOIN yarn_types y ON y.id = i.yarn_type_id WHERE i.order_id = ?""", (r["order_id"],))),
            "qc_history": rows(db.execute("SELECT q.*, u.username inspector FROM qc_records q LEFT JOIN users u "
                                          "ON u.id = q.inspector_id WHERE roll_id = ? ORDER BY q.id", (r["id"],))),
            "dispatch": dict(d) if (d := db.execute("SELECT d.* FROM dispatch_items i JOIN dispatches d "
                                                    "ON d.id = i.dispatch_id WHERE i.roll_id = ?",
                                                    (r["id"],)).fetchone()) else None}


# ---------- Quality control ----------

class QcIn(BaseModel):
    result: Literal["pass", "rework", "reject"]
    defect_type: Literal["hole", "colour_mismatch", "uneven_weave", "stain", "other"] | None = None
    notes: str | None = None


@app.get("/qc/queue")
def qc_queue(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("""SELECT r.*, m.name machine FROM rolls r JOIN machines m ON m.id = r.machine_id
        WHERE r.status IN ('pending_qc', 'rework') ORDER BY r.id"""))


@app.post("/qc/{qr}")
def qc_check(qr: str, body: QcIn, db=Depends(get_db), u=role("admin", "qc")):
    if body.result != "pass" and not (body.defect_type or body.notes):
        raise HTTPException(422, "give a defect type or notes for rework/reject")
    with tx(db):
        r = one(db, "SELECT * FROM rolls WHERE qr = ?", qr)
        if r["status"] not in ("pending_qc", "rework"):
            raise HTTPException(409, f"roll is {r['status']}, not awaiting QC")
        qid = insert(db, "qc_records", {"roll_id": r["id"], **body.model_dump(), "inspector_id": u["id"]})
        db.execute("UPDATE rolls SET status = ? WHERE id = ?",
                   ({"pass": "passed", "rework": "rework", "reject": "rejected"}[body.result], r["id"]))
    ai_result = None
    if body.result != "pass" and body.notes:
        m = one(db, "SELECT name, kind FROM machines WHERE id = ?", r["machine_id"])
        ai_result = decide(db, "defect", qid, {"note": body.notes, "machine": m["name"], "machine_kind": m["kind"]},
                           skip={"defect_type"} if body.defect_type else ())
    return {"qc": one(db, "SELECT * FROM qc_records WHERE id = ?", qid), "classification": ai_result}


# ---------- Finished goods & dispatch ----------

class DispatchIn(BaseModel):
    order_id: int
    vehicle_no: str = Field(min_length=1)
    driver: str = Field(min_length=1)


class ScanIn(BaseModel):
    qr: str


@app.get("/finished-goods")
def finished_goods(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("""SELECT o.id order_id, b.name buyer, f.name fabric, o.colour,
          COUNT(*) rolls, SUM(r.metres) metres
        FROM rolls r JOIN orders o ON o.id = r.order_id JOIN buyers b ON b.id = o.buyer_id
        JOIN fabric_types f ON f.id = o.fabric_type_id
        WHERE r.status = 'passed' GROUP BY o.id ORDER BY o.id"""))


@app.post("/dispatches")
def create_dispatch(body: DispatchIn, db=Depends(get_db), _=role("admin", "store")):
    return one(db, "SELECT * FROM dispatches WHERE id = ?", insert(db, "dispatches", body.model_dump()))


@app.post("/dispatches/{did}/scan")
def scan_roll(did: int, body: ScanIn, db=Depends(get_db), _=role("admin", "store")):
    """Hard guards, no AI: open dispatch, not already dispatched, right order, QC-passed."""
    with tx(db):
        d = one(db, "SELECT * FROM dispatches WHERE id = ?", did)
        if d["status"] != "open":
            raise HTTPException(409, "dispatch is closed")
        r = db.execute("SELECT * FROM rolls WHERE qr = ?", (body.qr,)).fetchone()
        if not r:
            raise HTTPException(404, "unknown roll")
        if on := db.execute("SELECT dispatch_id FROM dispatch_items WHERE roll_id = ?", (r["id"],)).fetchone():
            raise HTTPException(409, f"BLOCKED: roll already scanned on dispatch #{on[0]}")
        if r["order_id"] != d["order_id"]:
            raise HTTPException(409, f"BLOCKED: roll belongs to order #{r['order_id']}, not #{d['order_id']}")
        if r["status"] != "passed":
            raise HTTPException(409, f"BLOCKED: roll is {r['status']}, not QC-passed")
        insert(db, "dispatch_items", {"dispatch_id": did, "roll_id": r["id"]})
    return dispatch_detail(did, db)


@app.post("/dispatches/{did}/close")
def close_dispatch(did: int, db=Depends(get_db), _=role("admin", "store")):
    """Closes the truck and generates the invoice + packing list from the scanned rolls."""
    with tx(db):
        d = one(db, "SELECT d.*, o.price_per_m, f.gst_rate FROM dispatches d JOIN orders o ON o.id = d.order_id "
                    "JOIN fabric_types f ON f.id = o.fabric_type_id WHERE d.id = ?", did)
        if d["status"] != "open":
            raise HTTPException(409, "dispatch already closed")
        metres = db.execute("SELECT SUM(r.metres) FROM dispatch_items i JOIN rolls r ON r.id = i.roll_id "
                            "WHERE i.dispatch_id = ?", (did,)).fetchone()[0]
        if not metres:
            raise HTTPException(409, "no rolls scanned")
        db.execute("UPDATE rolls SET status = 'dispatched' WHERE id IN "
                   "(SELECT roll_id FROM dispatch_items WHERE dispatch_id = ?)", (did,))
        db.execute("UPDATE dispatches SET status = 'closed' WHERE id = ?", (did,))
        subtotal = round(metres * d["price_per_m"], 2)
        gst = round(subtotal * d["gst_rate"] / 100, 2)
        insert(db, "invoices", {"dispatch_id": did, "order_id": d["order_id"], "metres": metres,
                                "rate": d["price_per_m"], "subtotal": subtotal, "gst_rate": d["gst_rate"],
                                "gst": gst, "total": round(subtotal + gst, 2)})
    return dispatch_detail(did, db)


@app.get("/dispatches")
def dispatch_history(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("""SELECT d.*, b.name buyer, i.id invoice_id, i.total FROM dispatches d
        JOIN orders o ON o.id = d.order_id JOIN buyers b ON b.id = o.buyer_id
        LEFT JOIN invoices i ON i.dispatch_id = d.id ORDER BY d.id DESC"""))


@app.get("/dispatches/{did}")
def dispatch_detail(did: int, db=Depends(get_db), _=Depends(user)):
    """Dispatch with packing list and invoice (once closed)."""
    d = one(db, """SELECT d.*, b.name buyer, b.gstin buyer_gstin, b.address buyer_address, f.name fabric, o.colour
        FROM dispatches d JOIN orders o ON o.id = d.order_id JOIN buyers b ON b.id = o.buyer_id
        JOIN fabric_types f ON f.id = o.fabric_type_id WHERE d.id = ?""", did)
    packing = rows(db.execute("SELECT r.qr, r.metres FROM dispatch_items i JOIN rolls r ON r.id = i.roll_id "
                              "WHERE i.dispatch_id = ? ORDER BY i.id", (did,)))
    inv = db.execute("SELECT * FROM invoices WHERE dispatch_id = ?", (did,)).fetchone()
    return {**d, "packing_list": packing, "rolls": len(packing), "metres": sum(p["metres"] for p in packing),
            "invoice": dict(inv) if inv else None}


# ---------- Workers & wages ----------

class AttendanceIn(BaseModel):
    shift_id: int
    work_date: date = Field(default_factory=date.today)
    worker_ids: list[int]


@app.post("/attendance")
def mark_attendance(body: AttendanceIn, db=Depends(get_db), _=role("admin", "supervisor")):
    with tx(db):
        for w in body.worker_ids:
            db.execute("INSERT OR IGNORE INTO attendance (worker_id, shift_id, work_date) VALUES (?, ?, ?)",
                       (w, body.shift_id, body.work_date.isoformat()))
    return rows(db.execute("SELECT * FROM attendance WHERE shift_id = ? AND work_date = ?",
                           (body.shift_id, body.work_date.isoformat())))


def wages(db, month):
    # ponytail: daily rate per day present (double shifts not paid extra; overtime is out of scope).
    out = rows(db.execute("""SELECT w.id, w.name, w.wage_type, w.rate,
          (SELECT COUNT(DISTINCT work_date) FROM attendance WHERE worker_id = w.id AND strftime('%Y-%m', work_date) = :m) days,
          (SELECT COALESCE(SUM(output), 0) FROM production_entries WHERE operator_id = w.id AND strftime('%Y-%m', entry_date) = :m) output
        FROM workers w ORDER BY w.id""", {"m": month}))
    for w in out:
        w["wage"] = round(w["rate"] * (w["days"] if w["wage_type"] == "daily" else w["output"]), 2)
    return out


@app.get("/wages")
def wages_report(month: str | None = None, db=Depends(get_db), _=role("admin")):
    w = wages(db, month_of(month))
    return {"month": month_of(month), "workers": w, "total": round(sum(x["wage"] for x in w), 2)}


# ---------- Accounts & owner dashboard ----------

class PaymentIn(BaseModel):
    party: Literal["buyer", "supplier"]
    party_id: int
    amount: float = Field(gt=0)
    invoice_id: int | None = None
    paid_on: date = Field(default_factory=date.today)
    ref: str | None = None


@app.post("/payments")
def record_payment(body: PaymentIn, db=Depends(get_db), _=role("admin")):
    if body.invoice_id:
        inv = one(db, "SELECT o.buyer_id FROM invoices i JOIN orders o ON o.id = i.order_id WHERE i.id = ?", body.invoice_id)
        if body.party != "buyer" or inv["buyer_id"] != body.party_id:
            raise HTTPException(422, "invoice does not belong to this buyer")
    return one(db, "SELECT * FROM payments WHERE id = ?", insert(db, "payments", body.model_dump(mode="json")))


def order_cost(db, oid):
    yarn = db.execute("SELECT COALESCE(SUM(i.qty_kg * COALESCE(s.avg_rate, 0)), 0) FROM material_issues i "
                      "JOIN yarn_stock s ON s.id = i.yarn_type_id WHERE i.order_id = ?", (oid,)).fetchone()[0]
    # Piece workers: output x rate. Daily workers: day rate split by their hours on each order that day.
    labour = db.execute("""SELECT COALESCE(SUM(CASE w.wage_type WHEN 'piece' THEN p.output * w.rate
          ELSE w.rate * p.hours / NULLIF((SELECT SUM(q.hours) FROM production_entries q
            WHERE q.operator_id = p.operator_id AND q.entry_date = p.entry_date), 0) END), 0)
        FROM production_entries p JOIN workers w ON w.id = p.operator_id WHERE p.order_id = ?""", (oid,)).fetchone()[0]
    overhead = db.execute("SELECT COALESCE(SUM(p.hours * m.overhead_per_hour), 0) FROM production_entries p "
                          "JOIN machines m ON m.id = p.machine_id WHERE p.order_id = ?", (oid,)).fetchone()[0]
    revenue = db.execute("SELECT COALESCE(SUM(subtotal), 0) FROM invoices WHERE order_id = ?", (oid,)).fetchone()[0]
    cost = yarn + labour + overhead
    return {"order_id": oid, "yarn": round(yarn, 2), "wages": round(labour, 2), "overhead": round(overhead, 2),
            "total_cost": round(cost, 2), "revenue": round(revenue, 2), "margin": round(revenue - cost, 2)}


@app.get("/reports/order-cost")
def order_costs(db=Depends(get_db), _=role()):
    return [order_cost(db, r[0]) for r in db.execute("SELECT id FROM orders ORDER BY id DESC")]


@app.get("/reports/order-cost/{oid}")
def order_cost_one(oid: int, db=Depends(get_db), _=role()):
    one(db, "SELECT id FROM orders WHERE id = ?", oid)
    return order_cost(db, oid)


@app.get("/reports/machines")
def machine_output(month: str | None = None, db=Depends(get_db), _=role()):
    return rows(db.execute("""SELECT m.id, m.name, m.kind,
          COALESCE(SUM(p.output), 0) output, COALESCE(SUM(p.hours), 0) hours,
          (SELECT COUNT(*) FROM machine_status_log l WHERE l.machine_id = m.id AND l.status = 'Breakdown'
            AND strftime('%Y-%m', l.at) = :m) breakdowns
        FROM machines m LEFT JOIN production_entries p ON p.machine_id = m.id AND strftime('%Y-%m', p.entry_date) = :m
        GROUP BY m.id ORDER BY m.kind, m.name""", {"m": month_of(month)}))


@app.get("/reports/receivables")
def receivables(db=Depends(get_db), _=role("admin")):
    return rows(db.execute("""SELECT i.*, b.id buyer_id, b.name buyer,
          COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = i.id), 0) paid,
          i.total - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = i.id), 0) outstanding
        FROM invoices i JOIN orders o ON o.id = i.order_id JOIN buyers b ON b.id = o.buyer_id ORDER BY i.id"""))


@app.get("/reports/payables")
def payables(db=Depends(get_db), _=role("admin")):
    """Supplier bills are the GRNs (qty x rate)."""
    return rows(db.execute("""SELECT s.id, s.name,
          COALESCE((SELECT SUM(qty_kg * rate) FROM grn WHERE supplier_id = s.id), 0) billed,
          COALESCE((SELECT SUM(amount) FROM payments WHERE party = 'supplier' AND party_id = s.id), 0) paid,
          COALESCE((SELECT SUM(qty_kg * rate) FROM grn WHERE supplier_id = s.id), 0)
            - COALESCE((SELECT SUM(amount) FROM payments WHERE party = 'supplier' AND party_id = s.id), 0) outstanding
        FROM suppliers s ORDER BY s.id"""))


@app.get("/reports/pnl")
def pnl(month: str | None = None, db=Depends(get_db), _=role()):
    m = month_of(month)
    q = lambda sql: db.execute(sql, (m,)).fetchone()[0]  # noqa: E731
    revenue = q("SELECT COALESCE(SUM(subtotal), 0) FROM invoices WHERE strftime('%Y-%m', issued_on) = ?")  # ex-GST
    yarn = q("SELECT COALESCE(SUM(i.qty_kg * COALESCE(s.avg_rate, 0)), 0) FROM material_issues i "
             "JOIN yarn_stock s ON s.id = i.yarn_type_id WHERE strftime('%Y-%m', i.issued_on) = ?")
    wage_total = sum(w["wage"] for w in wages(db, m))
    overhead = q("SELECT COALESCE(SUM(p.hours * mc.overhead_per_hour), 0) FROM production_entries p "
                 "JOIN machines mc ON mc.id = p.machine_id WHERE strftime('%Y-%m', p.entry_date) = ?")
    costs = yarn + wage_total + overhead
    return {"month": m, "revenue": round(revenue, 2), "yarn": round(yarn, 2), "wages": round(wage_total, 2),
            "overhead": round(overhead, 2), "total_costs": round(costs, 2), "profit": round(revenue - costs, 2)}


# ---------- Buyer messages ----------

class MessageIn(BaseModel):
    text: str = Field(min_length=1)
    channel: Literal["whatsapp", "email", "phone"] = "whatsapp"
    buyer_id: int | None = None
    order_id: int | None = None


class DraftIn(BaseModel):
    lang: Literal["en", "ta", "hi"] = "en"


class ReplyIn(BaseModel):
    text: str = Field(min_length=1)


def message_context(db, msg):
    if msg["order_id"]:
        return [tracking(db, one(db, "SELECT * FROM orders WHERE id = ?", msg["order_id"]))]
    if msg["buyer_id"]:
        return [tracking(db, o) for o in rows(db.execute("SELECT * FROM orders WHERE buyer_id = ?", (msg["buyer_id"],)))]
    return []


@app.post("/messages")
def receive_message(body: MessageIn, db=Depends(get_db), _=role("admin")):
    mid = insert(db, "buyer_messages", body.model_dump())
    msg = one(db, "SELECT * FROM buyer_messages WHERE id = ?", mid)
    routing = decide(db, "message", mid, {"text": body.text, "orders": message_context(db, msg)})
    db.execute("UPDATE buyer_messages SET confidence = ? WHERE id = ?", (routing["confidence"], mid))
    return {"message": one(db, "SELECT * FROM buyer_messages WHERE id = ?", mid), "routing": routing}


@app.get("/messages")
def list_messages(status: str | None = None, dept: str | None = None, db=Depends(get_db), _=role("admin")):
    return rows(db.execute("SELECT * FROM buyer_messages WHERE (? IS NULL OR status = ?) AND (? IS NULL OR dept = ?) "
                           "ORDER BY id DESC", (status, status, dept, dept)))


@app.post("/messages/{mid}/draft")
def draft_reply(mid: int, body: DraftIn, db=Depends(get_db), _=role("admin")):
    msg = one(db, "SELECT * FROM buyer_messages WHERE id = ?", mid)
    try:
        text = ai.draft_reply(msg["text"], {"orders": message_context(db, msg)}, body.lang)
    except Exception as e:
        raise HTTPException(503, f"reply drafter unavailable: {e}")
    db.execute("UPDATE buyer_messages SET reply = ? WHERE id = ?", (text, mid))
    return {"draft": text}


@app.post("/messages/{mid}/reply")
def mark_replied(mid: int, body: ReplyIn, db=Depends(get_db), _=role("admin")):
    """Records the reply that was sent. Delivery over WhatsApp/email is the integration's job."""
    one(db, "SELECT id FROM buyer_messages WHERE id = ?", mid)
    db.execute("UPDATE buyer_messages SET reply = ?, status = 'replied' WHERE id = ?", (body.text, mid))
    return one(db, "SELECT * FROM buyer_messages WHERE id = ?", mid)
