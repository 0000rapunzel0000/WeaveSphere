"""Buyer orders: stage roll-up, yarn feasibility, deadline risk, and the public buyer tracking page."""
import math
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from mill import ai
from mill.auth import role, user
from mill.db import get_db, insert, one, rows
from mill.purchasing import auto_po

router = APIRouter()


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
          COALESCE(SUM(p.output) FILTER (WHERE p.entry_date >= date('now', 'localtime', '-6 day')), 0) week,
          COUNT(DISTINCT p.entry_date) FILTER (WHERE p.entry_date >= date('now', 'localtime', '-6 day')) days
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


@router.post("/orders")
def create_order(body: OrderIn, db=Depends(get_db), _=role("admin")):
    oid = insert(db, "orders", {**body.model_dump(mode="json"), "token": secrets.token_urlsafe(16)})
    return {**order_detail(oid, db), "po_refresh": auto_po(db)}


@router.get("/orders")
def list_orders(buyer_id: int | None = None, db=Depends(get_db), _=Depends(user)):
    orders = rows(db.execute("SELECT o.*, b.name buyer FROM orders o JOIN buyers b ON b.id = o.buyer_id "
                             "WHERE ? IS NULL OR o.buyer_id = ? ORDER BY o.id DESC", (buyer_id, buyer_id)))
    return [{**o, **tracking(db, o)} for o in orders]


@router.get("/orders/{oid}")
def order_detail(oid: int, db=Depends(get_db), _=Depends(user)):
    """Internal enquiry view: everything office staff need to answer a call."""
    o = one(db, "SELECT o.*, b.name buyer, b.phone buyer_phone FROM orders o JOIN buyers b ON b.id = o.buyer_id "
                "WHERE o.id = ?", oid)
    return {**o, **tracking(db, o), "tracking_path": f"/track/{o['token']}",
            "feasibility": feasibility(db, o),
            "job_cards": rows(db.execute("SELECT j.*, m.name machine, m.kind stage FROM job_cards j "
                                         "JOIN machines m ON m.id = j.machine_id WHERE j.order_id = ?", (oid,))),
            "invoices": rows(db.execute("SELECT * FROM invoices WHERE order_id = ?", (oid,)))}


@router.get("/orders/{oid}/feasibility")
def order_feasibility(oid: int, db=Depends(get_db), _=Depends(user)):
    return feasibility(db, one(db, "SELECT * FROM orders WHERE id = ?", oid))


@router.get("/orders/{oid}/risk")
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


@router.get("/track/{token}")
def track(token: str, db=Depends(get_db)):
    """Public buyer tracking link, no login."""
    return tracking(db, one(db, "SELECT * FROM orders WHERE token = ?", token))
