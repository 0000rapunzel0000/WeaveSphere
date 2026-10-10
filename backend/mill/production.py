"""Production floor: job cards, machine status, shift output, the floor board and QR-coded rolls."""
import secrets
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mill.auth import role, user
from mill.config import SIM_SPEED
from mill.db import get_db, insert, one, rows, tx
from mill.livefloor import accrue, crew, stage_done, ts, utcnow, yarn_at_loom
from mill.orders import tracking
from mill.reviews import decide

router = APIRouter()


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


@router.post("/jobs")
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


@router.post("/jobs/{jid}/done")
def finish_job(jid: int, db=Depends(get_db), _=role("admin", "supervisor")):
    with tx(db):
        accrue(db)
        db.execute("UPDATE job_cards SET status = 'done', last_tick = NULL WHERE id = ?", (jid,))
    return one(db, "SELECT * FROM job_cards WHERE id = ?", jid)


class ReleaseIn(BaseModel):
    job_ids: list[int] | None = None  # which of their jobs; default all open ones


@router.post("/workers/{wid}/release")
def release_worker(wid: int, body: ReleaseIn, db=Depends(get_db), _=role("admin", "supervisor")):
    """Free a worker from the jobs they run now (to move them elsewhere). A running machine stops, with a note, until
    someone else is put on its job: live output is never credited to a person who isn't there."""
    w = one(db, "SELECT * FROM workers WHERE id = ?", wid)
    out = []
    with tx(db):
        accrue(db)  # their output up to now stays theirs
        jobs = rows(db.execute("SELECT j.id, j.order_id, j.machine_id, m.name machine, m.status FROM job_cards j "
                               "JOIN machines m ON m.id = j.machine_id WHERE j.status = 'open' AND j.operator_id = ?", (wid,)))
        for j in jobs:
            if body.job_ids is not None and j["id"] not in body.job_ids:
                continue
            db.execute("UPDATE job_cards SET operator_id = NULL, last_tick = NULL, released = 1 WHERE id = ?", (j["id"],))
            stopped = j["status"] == "Running"
            if stopped:
                db.execute("UPDATE machines SET status = 'Idle' WHERE id = ?", (j["machine_id"],))
                insert(db, "machine_status_log", {"machine_id": j["machine_id"], "status": "Idle",
                                                  "note": f"stopped: {w['name']} was moved off this job; put an operator on it to restart"})
            out.append({"job_id": j["id"], "machine": j["machine"], "order_id": j["order_id"], "stopped": stopped})
    return {"worker": w["name"], "released": out}


@router.post("/machines/{mid}/status")
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


@router.get("/machines/{mid}/log")
def machine_log(mid: int, db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT * FROM machine_status_log WHERE machine_id = ? ORDER BY id DESC", (mid,)))


@router.post("/production")
def log_production(body: ProductionIn, db=Depends(get_db), _=role("admin", "supervisor")):
    return one(db, "SELECT * FROM production_entries WHERE id = ?",
               insert(db, "production_entries", body.model_dump(mode="json")))


@router.get("/production")
def list_production(entry_date: date | None = None, db=Depends(get_db), _=Depends(user)):
    d = entry_date and entry_date.isoformat()
    return rows(db.execute("""SELECT p.*, m.name machine, w.name operator, s.name shift FROM production_entries p
        JOIN machines m ON m.id = p.machine_id JOIN workers w ON w.id = p.operator_id JOIN shifts s ON s.id = p.shift_id
        WHERE ? IS NULL OR p.entry_date = ? ORDER BY p.id DESC LIMIT 200""", (d, d)))


@router.get("/floor")
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


def roll_code(scanned: str) -> str:
    """A roll's code from whatever was scanned or typed: the code itself, or the label's link (…/#roll=ETM-…),
    which is what a scanner gun types when it reads the QR."""
    from urllib.parse import unquote
    v = (scanned or "").strip()
    return unquote(v.split("roll=", 1)[1]).split("&")[0].strip() if "roll=" in v else v


@router.post("/rolls")
def create_roll(body: RollIn, db=Depends(get_db), _=role("admin", "supervisor")):
    """The QR code encodes `qr`; the client renders it as an image."""
    rid = insert(db, "rolls", {**body.model_dump(), "qr": "ETM-" + secrets.token_hex(5).upper()})
    return one(db, "SELECT * FROM rolls WHERE id = ?", rid)


@router.get("/rolls")
def list_rolls(order_id: int | None = None, status: Literal["pending_qc", "passed", "rework", "rejected", "dispatched"] | None = None,
               db=Depends(get_db), _=Depends(user)):
    """Rolls for pick lists, e.g. the passed rolls of an order waiting for a truck."""
    return rows(db.execute("SELECT * FROM rolls WHERE (? IS NULL OR order_id = ?) AND (? IS NULL OR status = ?) "
                           "ORDER BY id LIMIT 500", (order_id, order_id, status, status)))


@router.get("/rolls/{qr:path}")
def trace_roll(qr: str, db=Depends(get_db), _=Depends(user)):
    """Traceability: roll -> order, machine, shift, operator, yarn issued, QC history, dispatch."""
    r = one(db, """SELECT r.*, m.name machine, m.kind stage, s.name shift, w.name operator, b.name buyer
        FROM rolls r JOIN machines m ON m.id = r.machine_id JOIN shifts s ON s.id = r.shift_id
        JOIN workers w ON w.id = r.operator_id JOIN orders o ON o.id = r.order_id JOIN buyers b ON b.id = o.buyer_id
        WHERE r.qr = ?""", roll_code(qr))
    return {**r,
            "order": tracking(db, one(db, "SELECT * FROM orders WHERE id = ?", r["order_id"])),
            "yarn_issued": rows(db.execute("""SELECT i.*, y.count, y.material, y.colour FROM material_issues i
                JOIN yarn_types y ON y.id = i.yarn_type_id WHERE i.order_id = ?""", (r["order_id"],))),
            "qc_history": rows(db.execute("SELECT q.*, u.username inspector FROM qc_records q LEFT JOIN users u "
                                          "ON u.id = q.inspector_id WHERE roll_id = ? ORDER BY q.id", (r["id"],))),
            "dispatch": dict(d) if (d := db.execute("SELECT d.* FROM dispatch_items i JOIN dispatches d "
                                                    "ON d.id = i.dispatch_id WHERE i.roll_id = ?",
                                                    (r["id"],)).fetchone()) else None}
