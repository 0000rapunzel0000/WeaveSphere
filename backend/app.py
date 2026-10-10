"""Everest Textile Mills backend. Run: uvicorn app:app --reload  (API docs at /docs)"""
import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# backend/.env (git-ignored) supplies OPENROUTER_API_KEY etc.; real environment variables win.
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    for _line in open(_env):
        _k, _, _v = _line.strip().partition("=")
        if _k and not _k.startswith("#") and _v:
            os.environ.setdefault(_k.strip(), _v.strip())

import ai  # noqa: E402  reads settings at import

SESSION_HOURS = float(os.environ.get("SESSION_HOURS", "24"))
SIM_SPEED = float(os.environ.get("SIM_SPEED", "1"))  # machine time per real time; e.g. 60 = an hour per minute for demos
ACCRUE_EVERY = float(os.environ.get("ACCRUE_EVERY", "2"))  # seconds between live-floor updates
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mill.db"))
app = FastAPI(title="Everest Textile Mills")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(","),
                   allow_methods=["*"], allow_headers=["*"])


def connect():
    db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def migrate(db):
    """Columns added after a database was first created; CREATE TABLE IF NOT EXISTS can't add them."""
    for table, col, ddl in [("material_issues", "rate", "REAL"), ("ai_reviews", "auto", "INTEGER NOT NULL DEFAULT 0"),
                            ("ai_reviews", "final", "TEXT"), ("machines", "rate_m_per_hour", "REAL NOT NULL DEFAULT 0"),
                            ("job_cards", "operator_id", "INTEGER REFERENCES workers"),
                            ("job_cards", "shift_id", "INTEGER REFERENCES shifts"), ("job_cards", "running_since", "TEXT"),
                            ("job_cards", "last_tick", "TEXT"), ("production_entries", "auto", "INTEGER NOT NULL DEFAULT 0")]:
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


_db = connect()
_db.execute("PRAGMA journal_mode = WAL")
migrate(_db)
_db.executescript(open(os.path.join(os.path.dirname(__file__), "db", "schema.sql")).read())
# Issues made before rates were stored get the all-time average they used to be costed at.
_db.execute("""UPDATE material_issues SET rate = (SELECT SUM(g.qty_kg * g.rate) / SUM(g.qty_kg) FROM grn g
               WHERE g.yarn_type_id = material_issues.yarn_type_id) WHERE rate IS NULL""")
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
    m = str(e)
    if m.startswith("UNIQUE"):
        return JSONResponse({"detail": "Already exists: " + m.split(": ", 1)[-1]}, status_code=409)
    if m.startswith("FOREIGN KEY"):
        return JSONResponse({"detail": "It refers to a record that does not exist."}, status_code=409)
    if m.startswith("NOT NULL"):
        return JSONResponse({"detail": m.split(": ", 1)[-1] + " is required."}, status_code=422)
    return JSONResponse({"detail": "Not allowed: " + m}, status_code=422)  # CHECK or wrong type


@app.exception_handler(RequestValidationError)
def bad_input(_, e):
    """One readable line, so `detail` is always a string."""
    return JSONResponse({"detail": "; ".join(f"{'.'.join(map(str, x['loc'][1:])) or 'body'}: {x['msg']}"
                                             for x in e.errors())}, status_code=422)


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
                   "WHERE s.token = ? AND s.created_at >= datetime('now', ?)",
                   (authorization.removeprefix("Bearer "), f"-{SESSION_HOURS} hours")).fetchone()
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
    username: str = Field(min_length=1, max_length=64, pattern=r"^\S+$")
    password: str = Field(min_length=8)
    role: Role


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


# ponytail: in-memory lockout, per process; move to a table if the API ever runs as several processes.
FAILED: dict[str, list[float]] = {}
LOCK_AFTER, LOCK_SECONDS = 5, 15 * 60


@app.post("/auth/setup")
def setup(body: Login, db=Depends(get_db)):
    """Creates the first owner. Only works on an empty database."""
    if len(body.password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")
    with tx(db):
        if db.execute("SELECT 1 FROM users").fetchone():
            raise HTTPException(403, "already set up")
        insert(db, "users", {"username": body.username.strip(), "pw_hash": hash_pw(body.password), "role": "owner"})
    return login(body, db)


@app.post("/auth/login")
def login(body: Login, db=Depends(get_db)):
    now, name = time.time(), body.username.strip().lower()
    recent = [t for t in FAILED.get(name, []) if now - t < LOCK_SECONDS]
    if len(recent) >= LOCK_AFTER:
        raise HTTPException(429, f"too many failed sign-ins; try again in {int((LOCK_SECONDS - (now - recent[0])) // 60) + 1} min")
    u = db.execute("SELECT * FROM users WHERE username = ?", (body.username.strip(),)).fetchone()
    # Hash even for unknown users so the response time doesn't reveal which usernames exist.
    stored = u["pw_hash"] if u else hash_pw("x")
    if not secrets.compare_digest(hash_pw(body.password, stored.split("$")[0]), stored) or not u:
        if len(FAILED) > 10_000:  # made-up usernames must not grow this forever
            FAILED.clear()
        FAILED[name] = recent + [now]
        raise HTTPException(401, "wrong username or password")
    FAILED.pop(name, None)
    db.execute("DELETE FROM sessions WHERE created_at < datetime('now', ?)", (f"-{SESSION_HOURS} hours",))
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
    colour: str = Field(min_length=1)
    qty_m: float = Field(gt=0)
    deadline: date
    price_per_m: float = Field(ge=0)


def roll_totals(db, oid):
    return dict(db.execute("""
        SELECT COUNT(*) rolls, COALESCE(SUM(metres), 0) produced_m,
          COALESCE(SUM(metres) FILTER (WHERE status = 'dispatched'), 0) dispatched_m,
          COUNT(*) FILTER (WHERE status IN ('passed', 'dispatched')) rolls_passed,
          COALESCE(SUM(metres) FILTER (WHERE status IN ('passed', 'dispatched')), 0) passed_m,
          COUNT(*) FILTER (WHERE status IN ('pending_qc', 'rework')) in_qc
        FROM rolls WHERE order_id = ? AND status != 'rejected'""", (oid,)).fetchone())


def order_status(db, o, t):
    # Dispatched only once the whole quantity has shipped; a part-shipped order is still being made.
    if t["dispatched_m"] >= o["qty_m"]:
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
    """Yarn this order still needs vs stock that earlier open orders haven't already claimed."""
    # ponytail: first come, first served by order id; add a priority column if the office needs to reorder.
    lines = rows(db.execute("""
        SELECT y.id yarn_type_id, y.count, y.material, y.colour, b.kg_per_m * :qty required_kg,
          COALESCE((SELECT SUM(qty_kg) FROM material_issues WHERE order_id = :oid AND yarn_type_id = y.id), 0) issued_kg,
          y.stock_kg available_kg,
          COALESCE((SELECT SUM(MAX(0, o2.qty_m * b2.kg_per_m - COALESCE((SELECT SUM(i.qty_kg) FROM material_issues i
                                                                       WHERE i.order_id = o2.id AND i.yarn_type_id = y.id), 0)))
            FROM orders o2 JOIN bom b2 ON b2.fabric_type_id = o2.fabric_type_id AND b2.yarn_type_id = y.id
            WHERE o2.id < :oid AND (SELECT COALESCE(SUM(r.metres), 0) FROM rolls r
                                    WHERE r.order_id = o2.id AND r.status = 'dispatched') < o2.qty_m), 0) reserved_kg
        FROM bom b JOIN yarn_stock y ON y.id = b.yarn_type_id WHERE b.fabric_type_id = :fid""",
                            {"qty": o["qty_m"], "oid": o["id"], "fid": o["fabric_type_id"]}))
    for line in lines:
        line["free_kg"] = round(max(0, line["available_kg"] - line["reserved_kg"]), 2)
        line["short_kg"] = round(max(0, line["required_kg"] - line["issued_kg"] - line["free_kg"]), 2)
    short = round(sum(line["short_kg"] for line in lines), 2)
    if not lines:
        verdict = "no BOM for this fabric type"
    else:
        verdict = "can start" if short == 0 else f"short by {short:g} kg"
    return {"can_start": bool(lines) and short == 0, "short_kg": short, "verdict": verdict, "lines": lines}


def stage_output(db, oid):
    """Metres logged on the furthest fabric stage the order has reached: total, last 7 days, active days."""
    # ponytail: spinning output is kg so it is skipped; per-stage targets would need a routing plan.
    r = db.execute("""SELECT SUM(p.output) total,
          COALESCE(SUM(p.output) FILTER (WHERE p.entry_date >= date('now', '-6 day')), 0) week,
          COUNT(DISTINCT p.entry_date) FILTER (WHERE p.entry_date >= date('now', '-6 day')) days
        FROM production_entries p JOIN machines m ON m.id = p.machine_id
        WHERE p.order_id = ? AND m.kind != 'spinning'
        GROUP BY m.kind ORDER BY CASE m.kind WHEN 'dyeing' THEN 2 ELSE 1 END DESC LIMIT 1""", (oid,)).fetchone()
    return dict(r) if r else {"total": 0, "week": 0, "days": 0}


def expected_ready(produced, qty, s):
    if produced >= qty:
        return date.today().isoformat()
    # ponytail: run rate per active day over the last week; swap for a per-stage plan if this proves too naive.
    rate = s["week"] / s["days"] if s["days"] else 0
    return (date.today() + timedelta(days=math.ceil((qty - produced) / rate))).isoformat() if rate else None


def tracking(db, o):
    """What a buyer may see. Also the order context fed to Jev and the reply drafter."""
    t = roll_totals(db, o["id"])
    s = stage_output(db, o["id"])
    produced = max(t["produced_m"], s["total"] or 0)  # counts fabric still on the loom, before any roll exists
    fabric = db.execute("SELECT name FROM fabric_types WHERE id = ?", (o["fabric_type_id"],)).fetchone()[0]
    return {
        "order_no": o["id"], "fabric": fabric, "colour": o["colour"], "stage": order_status(db, o, t),
        "target_m": o["qty_m"], "produced_m": round(produced, 2), "passed_m": t["passed_m"], "dispatched_m": t["dispatched_m"],
        "rolls_produced": t["rolls"], "rolls_passed": t["rolls_passed"],
        "progress_pct": round(100 * min(produced / o["qty_m"], 1), 1),
        "deadline": o["deadline"], "expected_ready": expected_ready(produced, o["qty_m"], s),
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
    p = ai.probability(state, "Will this order miss its deadline?",
                       "the order is likely to finish after its deadline", "the order will finish on time")
    # probability is None when Jev is unavailable; the frontend shows "unknown".
    return {**state, "at_risk": None if p is None else p >= 0.5, "probability": None if p is None else round(p, 3)}


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
    image_b64: str = Field(min_length=1, max_length=14_000_000)  # ~10 MB photo
    mime: Literal["image/jpeg", "image/png", "image/webp", "image/heic"] = "image/jpeg"


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
    norm = lambda v: str(v or "").strip().lower()  # noqa: E731  exact match, ignoring case and spaces
    supplier = db.execute("SELECT id FROM suppliers WHERE lower(trim(name)) = ?", (norm(f.get("supplier")),)).fetchone()
    yarn = db.execute("SELECT id FROM yarn_types WHERE lower(trim(count)) = ? AND lower(trim(material)) = ? "
                      "AND lower(trim(colour)) = ?",
                      (norm(f.get("yarn_count")), norm(f.get("material")), norm(f.get("colour")))).fetchone()
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
        s = one(db, "SELECT stock_kg, avg_rate FROM yarn_stock WHERE id = ?", body.yarn_type_id)
        if body.qty_kg - s["stock_kg"] > 1e-6:  # float sums: issuing exactly what's on hand must work
            raise HTTPException(409, f"only {s['stock_kg']:g} kg in stock")
        iid = insert(db, "material_issues", {**body.model_dump(), "rate": s["avg_rate"], "created_by": u["id"]})
    return {"issue": one(db, "SELECT * FROM material_issues WHERE id = ?", iid),
            "low_stock": rows(db.execute(LOW_STOCK))}


@app.get("/issues")
def list_issues(order_id: int | None = None, db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT * FROM material_issues WHERE ? IS NULL OR order_id = ? ORDER BY id DESC",
                           (order_id, order_id)))


# ---------- AI decisions + human review queue ----------

# Option descriptions are what Jev reads, so they decide how sure it can be. Edit them, not the code.
HEADS = {  # kind -> (table, {field: (Jev instructions, {option: description})}); first field is the one the gate checks
    "message": ("buyer_messages", {
        "dept": ("Which mill department should handle this buyer message?", {
            "Office": "general enquiries, documents, order confirmations, anything the office can answer",
            "QC": "complaints about fabric quality, defects, shade, weight or width",
            "Production": "when goods will be ready, how far an order has progressed in the mill",
            "Dispatch": "trucks, delivery dates and addresses, missing or delayed shipments",
            "Accounts": "invoices, payments, dues, GST, credit notes"}),
        "intent": ("What does the buyer want?", {
            "status": "asking where an order is or when it will be ready",
            "quality_complaint": "complaining about defects, colour, weight or quality of fabric received",
            "payment": "about invoices, payments, dues or GST",
            "new_order": "wants to place a new order or get a quote",
            "delivery_change": "wants to change the delivery date, address or quantity",
            "other": "greetings, acknowledgements, or anything unclear that fits none of the above"}),
        "urgency": ("How urgently does the mill need to respond?", {
            "low": "nothing is at stake, it can wait a day",
            "med": "should be answered today",
            "high": "the buyer's production, a deadline or money is at risk; answer within the hour"})}),
    "breakdown": ("machine_status_log", {
        "category": ("What kind of fault caused this machine breakdown?", {
            "mechanical": "belts, bearings, gears, shuttle or needle damage, jams, worn or broken parts",
            "electrical": "motor, wiring, power trips, sensors, drives, control panel faults",
            "yarn_quality": "yarn snapping, slubs, weak or uneven yarn, knots, wrong count",
            "operator": "set-up or handling mistake, wrong settings, threading error"}),
        "severity": ("How badly does this breakdown hurt production?", {
            "low": "the machine can keep running or be fixed in minutes",
            "med": "the machine is stopped for a few hours",
            "high": "the machine is stopped for a shift or more, or an order deadline is at risk"})}),
    "defect": ("qc_records", {
        "defect_type": ("What kind of fabric defect does the inspector describe?", {
            "hole": "holes, tears, missing threads, broken picks",
            "colour_mismatch": "shade differs from the approved colour, patchy or uneven dyeing",
            "uneven_weave": "irregular density or tension, bars, slubs, pattern faults",
            "stain": "oil, dirt, dye or water marks",
            "other": "anything that fits none of the above"}),
        "stage_at_fault": ("Which production stage most likely caused the defect?", {
            "spinning": "caused while making the yarn (count, twist, slubs)",
            "weaving": "caused on the loom (holes, tension, broken ends)",
            "dyeing": "caused in dyeing or finishing (colour, stains, shade)"}),
        "severity": ("How serious is this defect for the buyer?", {
            "low": "cosmetic, within normal tolerance",
            "med": "clearly visible, the buyer may demand a discount or rework",
            "high": "unsellable, the buyer would reject the roll"})}),
}
# Who fixes a breakdown follows from its category: a rule, not an AI call.
ASSIGNEE_FOR = {"mechanical": "fitter", "electrical": "electrician", "yarn_quality": "store", "operator": "supervisor"}


def apply_decision(db, kind, ref_id, fields):
    table = HEADS[kind][0]
    if kind == "breakdown" and "category" in fields:
        fields = {**fields, "assignee": ASSIGNEE_FOR[fields["category"]]}
    sets = [f"{k} = ?" for k in fields] + (["status = CASE status WHEN 'replied' THEN status ELSE 'routed' END"]
                                           if kind == "message" else [])
    db.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", (*fields.values(), ref_id))


def decide(db, kind, ref_id, state, skip=()):
    """Confidence gate on the primary decision (dept / category / defect); the other fields ride along as hints.
    A message Jev can only call "other" can't be routed, so a human sees it whatever the confidence."""
    res = ai.decide(state, {f: q for f, q in HEADS[kind][1].items() if f not in skip})
    choice = {f: r["choice"] for f, r in res.items()}
    conf = next(iter(res.values()))["confidence"]
    if kind == "breakdown":
        choice["assignee"] = ASSIGNEE_FOR[choice["category"]]
    auto = conf >= ai.THRESHOLD and choice.get("intent") != "other"
    if auto:
        apply_decision(db, kind, ref_id, {f: v for f, v in choice.items() if f in res})
    rid = insert(db, "ai_reviews", {"kind": kind, "ref_id": ref_id, "suggestion": json.dumps(choice), "confidence": conf,
                                    "auto": int(auto), "status": "resolved" if auto else "open"})
    return {"auto": auto, "confidence": round(conf, 3), "suggestion": choice, "review_id": rid,
            "probs": {f: r["probs"] for f, r in res.items()}}


def review_row(r):
    return {**r, "auto": bool(r["auto"]), "suggestion": json.loads(r["suggestion"]),
            "final": json.loads(r["final"]) if r["final"] else None}


@app.get("/reviews")
def review_queue(status: Literal["open", "resolved", "all"] = "open", kind: str | None = None,
                 db=Depends(get_db), _=role("admin", "supervisor", "qc")):
    """open = the human queue; resolved = auto-applied or already decided (still correctable)."""
    return [review_row(r) for r in rows(db.execute(
        "SELECT * FROM ai_reviews WHERE (? = 'all' OR status = ?) AND (? IS NULL OR kind = ?) ORDER BY id DESC",
        (status, status, kind, kind)))]


@app.get("/reviews/accuracy")
def review_accuracy(db=Depends(get_db), _=role("admin")):
    """How often Jev's main decision matched what a person settled on, by confidence band.
    This is the evidence for AI_THRESHOLD: pick the lowest band whose accuracy you can live with."""
    bands = [(0.9, 1.01, "0.9-1.0"), (0.8, 0.9, "0.8-0.9"), (0.7, 0.8, "0.7-0.8"), (0.0, 0.7, "below 0.7")]
    out = {}
    for r in rows(db.execute("SELECT * FROM ai_reviews WHERE confidence > 0")):  # 0 = AI was down, not a decision
        k = out.setdefault(r["kind"], {"decisions": 0, "auto_applied": 0, "human_checked": 0,
                                       "bands": {b: {"checked": 0, "agreed": 0} for _, _, b in bands}})
        k["decisions"] += 1
        k["auto_applied"] += r["auto"]
        if r["final"]:
            suggestion, final = json.loads(r["suggestion"]), json.loads(r["final"])
            gate = next(f for f in HEADS[r["kind"]][1] if f in suggestion)  # the field the gate checked
            band = next(b for lo, hi, b in bands if lo <= r["confidence"] < hi)
            k["human_checked"] += 1
            k["bands"][band]["checked"] += 1
            k["bands"][band]["agreed"] += final.get(gate) == suggestion[gate]
    for k in out.values():
        for b in k["bands"].values():
            b["accuracy"] = round(b["agreed"] / b["checked"], 3) if b["checked"] else None
    return {"threshold": ai.THRESHOLD, "kinds": out}


@app.post("/reviews/{rid}")
def resolve_review(rid: int, fields: dict[str, str] | None = None, db=Depends(get_db),
                   _=role("admin", "supervisor", "qc")):
    """Open item: an empty body accepts the AI's suggestion; fields sent correct it.
    Settled item (auto-applied or already decided): send the fields to correct it."""
    r = one(db, "SELECT * FROM ai_reviews WHERE id = ?", rid)
    if r["status"] != "open" and not fields:
        raise HTTPException(409, "This decision is already settled. Send the corrected fields to change it.")
    allowed = HEADS[r["kind"]][1]
    for f, v in (fields or {}).items():
        if f not in allowed:
            raise HTTPException(422, f"Unknown field '{f}'. Allowed: {', '.join(allowed)}.")
        if v not in allowed[f][1]:
            raise HTTPException(422, f"{f} must be one of: {', '.join(allowed[f][1])}.")
    current = json.loads(r["final"] or r["suggestion"])
    applied = {f: v for f, v in {**current, **(fields or {})}.items() if f in allowed}
    with tx(db):
        apply_decision(db, r["kind"], r["ref_id"], applied)
        db.execute("UPDATE ai_reviews SET status = 'resolved', final = ? WHERE id = ?", (json.dumps(applied), rid))
    return {"ok": True, "applied": applied}


# ---------- Live floor: running machines produce output and use yarn ----------

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ts(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")  # same format as SQLite CURRENT_TIMESTAMP (UTC)


def stage_done(db, oid, kind):
    return db.execute("SELECT COALESCE(SUM(p.output), 0) FROM production_entries p JOIN machines m ON m.id = p.machine_id "
                      "WHERE p.order_id = ? AND m.kind = ?", (oid, kind)).fetchone()[0]


def yarn_at_loom(db, oid):
    """Per recipe yarn: kg issued to the order but not yet woven (issued - woven metres x kg/m), plus store stock."""
    return rows(db.execute("""
        SELECT b.yarn_type_id, b.kg_per_m, s.stock_kg, s.avg_rate, s.count, s.material, s.colour,
          COALESCE((SELECT SUM(qty_kg) FROM material_issues WHERE order_id = :o AND yarn_type_id = b.yarn_type_id), 0)
            - :woven * b.kg_per_m buffer_kg
        FROM orders o JOIN bom b ON b.fabric_type_id = o.fabric_type_id JOIN yarn_stock s ON s.id = b.yarn_type_id
        WHERE o.id = :o""", {"o": oid, "woven": stage_done(db, oid, "weaving")}))


def crew(db, job):
    """Operator and shift that live output is credited to: the job card's, else whoever last ran the machine."""
    op, sh = job["operator_id"], job["shift_id"]
    if not (op and sh):
        last = db.execute("SELECT operator_id, shift_id FROM production_entries WHERE machine_id = ? ORDER BY id DESC "
                          "LIMIT 1", (job["machine_id"],)).fetchone()
        op, sh = op or (last and last[0]), sh or (last and last[1])
    return op, sh


def stop_machine(db, job, why, done=False):
    db.execute("UPDATE machines SET status = 'Idle' WHERE id = ?", (job["machine_id"],))
    db.execute("UPDATE job_cards SET last_tick = NULL, status = ? WHERE id = ?", ("done" if done else "open", job["id"]))
    insert(db, "machine_status_log", {"machine_id": job["machine_id"], "status": "Idle", "note": why})


def accrue(db):
    """Bring every running machine up to now. Caller holds the write lock.
    Output = speed x elapsed machine time, capped by what the order still needs at that stage and, on looms,
    by yarn: first the yarn already issued to the order, then store stock (issued automatically, at the moving
    average rate). Out of yarn or stage complete -> the machine stops and the log says why."""
    # ponytail: spinning output is kg of yarn, not metres; it is not run live.
    now = utcnow()
    for j in rows(db.execute("""SELECT j.*, m.kind, m.rate_m_per_hour rate, o.qty_m FROM job_cards j
            JOIN machines m ON m.id = j.machine_id JOIN orders o ON o.id = j.order_id
            WHERE j.status = 'open' AND j.last_tick IS NOT NULL AND m.status = 'Running'
              AND m.rate_m_per_hour > 0 AND m.kind != 'spinning'""")):
        hours = max(0.0, (now - datetime.strptime(j["last_tick"][:19], "%Y-%m-%d %H:%M:%S")).total_seconds()) / 3600 * SIM_SPEED
        remaining = max(0.0, j["qty_m"] - stage_done(db, j["order_id"], j["kind"]))
        metres, why = min(j["rate"] * hours, remaining), None
        op, sh = crew(db, j)
        if not (op and sh):
            stop_machine(db, j, "stopped: give the job card an operator and shift to record live output")
            continue
        if j["kind"] == "weaving" and metres > 0:
            lines = yarn_at_loom(db, j["order_id"])
            for x in lines:
                can = (max(0, x["buffer_kg"]) + max(0, x["stock_kg"])) / x["kg_per_m"]
                if can < metres - 1e-9:
                    metres, why = max(0.0, can), f"stopped: out of {x['count']} {x['material']} {x['colour']} yarn"
            for x in lines:
                draw = min(metres * x["kg_per_m"] - max(0, x["buffer_kg"]), max(0, x["stock_kg"]))
                if draw > 1e-6:
                    insert(db, "material_issues", {"order_id": j["order_id"], "yarn_type_id": x["yarn_type_id"],
                                                   "machine_id": j["machine_id"], "qty_kg": draw, "rate": x["avg_rate"]})
        if metres > 0:
            day, used = date.today().isoformat(), metres / j["rate"]
            key = (j["machine_id"], j["order_id"], op, sh, day)
            e = db.execute("SELECT id FROM production_entries WHERE auto = 1 AND machine_id = ? AND order_id = ? "
                           "AND operator_id = ? AND shift_id = ? AND entry_date = ?", key).fetchone()
            if e:
                db.execute("UPDATE production_entries SET output = output + ?, hours = hours + ? WHERE id = ?",
                           (metres, used, e[0]))
            else:
                insert(db, "production_entries", {"machine_id": key[0], "order_id": key[1], "operator_id": op,
                                                  "shift_id": sh, "entry_date": day, "output": metres, "hours": used,
                                                  "auto": 1})
        if remaining - metres <= 1e-6:
            stop_machine(db, j, f"finished: order #{j['order_id']} {j['kind']} complete", done=True)
        elif why:
            stop_machine(db, j, why)
        else:
            db.execute("UPDATE job_cards SET last_tick = ? WHERE id = ?", (ts(now), j["id"]))


_last_accrual = 0.0


def accrue_now():
    """Live-floor update before a request is served, at most every ACCRUE_EVERY seconds. Never blocks a request."""
    global _last_accrual
    if time.time() - _last_accrual < ACCRUE_EVERY:
        return
    _last_accrual = time.time()
    db = connect()
    try:
        if db.execute("SELECT 1 FROM job_cards WHERE status = 'open' AND last_tick IS NOT NULL LIMIT 1").fetchone():
            with tx(db):
                accrue(db)
    except Exception:
        logging.exception("live floor update failed")
    finally:
        db.close()


@app.middleware("http")
async def live_floor(request, call_next):
    await run_in_threadpool(accrue_now)
    return await call_next(request)


# ---------- Production floor ----------

class JobIn(BaseModel):
    order_id: int
    machine_id: int
    operator_id: int | None = None  # live output is credited to them; defaults to the machine's last operator
    shift_id: int | None = None


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
        accrue(db)  # credit the outgoing job up to now
        db.execute("UPDATE job_cards SET status = 'done', last_tick = NULL WHERE machine_id = ? AND status = 'open'",
                   (body.machine_id,))
        running = one(db, "SELECT status FROM machines WHERE id = ?", body.machine_id)["status"] == "Running"
        now = ts(utcnow())
        jid = insert(db, "job_cards", {**body.model_dump(), "running_since": now if running else None,
                                       "last_tick": now if running else None})
    return one(db, "SELECT * FROM job_cards WHERE id = ?", jid)


@app.post("/jobs/{jid}/done")
def finish_job(jid: int, db=Depends(get_db), _=role("admin", "supervisor")):
    with tx(db):
        accrue(db)
        db.execute("UPDATE job_cards SET status = 'done', last_tick = NULL WHERE id = ?", (jid,))
    return one(db, "SELECT * FROM job_cards WHERE id = ?", jid)


@app.post("/machines/{mid}/status")
def machine_status(mid: int, body: StatusIn, db=Depends(get_db), _=role("admin", "supervisor")):
    m = one(db, "SELECT * FROM machines WHERE id = ?", mid)
    with tx(db):
        accrue(db)  # count output up to the moment the status changes
        job = db.execute("SELECT * FROM job_cards WHERE machine_id = ? AND status = 'open'", (mid,)).fetchone()
        if body.status == "Running" and job and m["rate_m_per_hour"] and m["kind"] != "spinning" and not all(crew(db, job)):
            raise HTTPException(422, f"{m['name']} needs an operator and shift on its job card before it can run. "
                                     "Re-assign the order with them in 'Assign order to machine'.")
        db.execute("UPDATE machines SET status = ? WHERE id = ?", (body.status, mid))
        if body.status == "Running":
            now = ts(utcnow())
            db.execute("UPDATE job_cards SET running_since = ?, last_tick = ? WHERE machine_id = ? AND status = 'open' "
                       "AND last_tick IS NULL", (now, now, mid))
        else:
            db.execute("UPDATE job_cards SET last_tick = NULL WHERE machine_id = ? AND status = 'open'", (mid,))
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


@app.get("/production")
def list_production(entry_date: date | None = None, db=Depends(get_db), _=Depends(user)):
    d = entry_date and entry_date.isoformat()
    return rows(db.execute("""SELECT p.*, m.name machine, w.name operator, s.name shift FROM production_entries p
        JOIN machines m ON m.id = p.machine_id JOIN workers w ON w.id = p.operator_id JOIN shifts s ON s.id = p.shift_id
        WHERE ? IS NULL OR p.entry_date = ? ORDER BY p.id DESC LIMIT 200""", (d, d)))


@app.get("/floor")
def floor(db=Depends(get_db), _=Depends(user)):
    machines = rows(db.execute("""
        SELECT m.*, j.id job_id, j.order_id, j.running_since, w.name operator, s.name shift, o.qty_m,
          (SELECT note FROM machine_status_log WHERE machine_id = m.id ORDER BY id DESC LIMIT 1) last_note,
          COALESCE((SELECT SUM(output) FROM production_entries WHERE machine_id = m.id AND entry_date = :d), 0) today_output
        FROM machines m LEFT JOIN job_cards j ON j.machine_id = m.id AND j.status = 'open'
        LEFT JOIN orders o ON o.id = j.order_id LEFT JOIN workers w ON w.id = j.operator_id LEFT JOIN shifts s ON s.id = j.shift_id
        ORDER BY m.kind, m.name""", {"d": date.today().isoformat()}))
    orders = {}
    for m in machines:
        m["idle"] = m["status"] != "Running"
        m["order_progress_pct"] = m["stage_done_m"] = m["finishes_at"] = None
        m["yarn_kg_per_hour"], m["yarn_at_loom"] = 0, []
        if m["order_id"]:
            if m["order_id"] not in orders:
                orders[m["order_id"]] = tracking(db, one(db, "SELECT * FROM orders WHERE id = ?", m["order_id"]))
            m["order_progress_pct"] = orders[m["order_id"]]["progress_pct"]
            m["stage_done_m"] = round(stage_done(db, m["order_id"], m["kind"]), 2)
            remaining = max(0, m["qty_m"] - m["stage_done_m"])
            if m["status"] == "Running" and m["rate_m_per_hour"] and m["kind"] != "spinning" and remaining:
                real_hours = remaining / m["rate_m_per_hour"] / SIM_SPEED
                m["finishes_at"] = ts(utcnow() + timedelta(hours=real_hours))
            if m["kind"] == "weaving":
                lines = yarn_at_loom(db, m["order_id"])
                m["yarn_kg_per_hour"] = round(m["rate_m_per_hour"] * sum(x["kg_per_m"] for x in lines), 3)
                m["yarn_at_loom"] = [{"yarn": f'{x["count"]} {x["material"]} · {x["colour"]}', "kg": round(max(0, x["buffer_kg"]), 2),
                                      "store_kg": round(x["stock_kg"], 2)} for x in lines]
    return {"machines": machines, "idle_count": sum(m["idle"] for m in machines), "orders": list(orders.values()),
            "sim_speed": SIM_SPEED}


@app.post("/rolls")
def create_roll(body: RollIn, db=Depends(get_db), _=role("admin", "supervisor")):
    """The QR code encodes `qr`; the client renders it as an image."""
    rid = insert(db, "rolls", {**body.model_dump(), "qr": "ETM-" + secrets.token_hex(5).upper()})
    return one(db, "SELECT * FROM rolls WHERE id = ?", rid)


@app.get("/rolls")
def list_rolls(order_id: int | None = None, status: Literal["pending_qc", "passed", "rework", "rejected", "dispatched"] | None = None,
               db=Depends(get_db), _=Depends(user)):
    """Rolls for pick lists, e.g. the passed rolls of an order waiting for a truck."""
    return rows(db.execute("SELECT * FROM rolls WHERE (? IS NULL OR order_id = ?) AND (? IS NULL OR status = ?) "
                           "ORDER BY id LIMIT 500", (order_id, order_id, status, status)))


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
        rec = body.model_dump()
        if body.result == "pass":
            rec["defect_type"] = None
        qid = insert(db, "qc_records", {"roll_id": r["id"], **rec, "inspector_id": u["id"]})
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
    if not db.execute(f"SELECT 1 FROM {body.party}s WHERE id = ?", (body.party_id,)).fetchone():  # party is a Literal
        raise HTTPException(422, f"no {body.party} with id {body.party_id}")
    if body.invoice_id:
        inv = one(db, "SELECT o.buyer_id FROM invoices i JOIN orders o ON o.id = i.order_id WHERE i.id = ?", body.invoice_id)
        if body.party != "buyer" or inv["buyer_id"] != body.party_id:
            raise HTTPException(422, "invoice does not belong to this buyer")
    return one(db, "SELECT * FROM payments WHERE id = ?", insert(db, "payments", body.model_dump(mode="json")))


def order_cost(db, oid):
    yarn = db.execute("SELECT COALESCE(SUM(qty_kg * COALESCE(rate, 0)), 0) FROM material_issues WHERE order_id = ?",
                      (oid,)).fetchone()[0]
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
    yarn = q("SELECT COALESCE(SUM(qty_kg * COALESCE(rate, 0)), 0) FROM material_issues "
             "WHERE strftime('%Y-%m', issued_on) = ?")
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
    """All of this buyer's orders (the one the message was logged against first), never another buyer's.
    Buyers often ask about a different order than the one the office picked."""
    if not msg["buyer_id"]:
        return []
    return [tracking(db, o) for o in rows(db.execute(
        "SELECT * FROM orders WHERE buyer_id = ? ORDER BY id = ? DESC, id", (msg["buyer_id"], msg["order_id"])))]


@app.post("/messages")
def receive_message(body: MessageIn, db=Depends(get_db), _=role("admin")):
    data = body.model_dump()
    if body.order_id:
        owner = one(db, "SELECT buyer_id FROM orders WHERE id = ?", body.order_id)["buyer_id"]
        if body.buyer_id and body.buyer_id != owner:
            raise HTTPException(422, f"Order #{body.order_id} belongs to a different buyer.")
        data["buyer_id"] = owner
    mid = insert(db, "buyer_messages", data)
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
