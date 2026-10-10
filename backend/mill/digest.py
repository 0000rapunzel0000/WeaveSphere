"""The owner's daily digest: facts computed by code, worded by the chat model (numbers checked)."""
import asyncio
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends

from mill import ai
from mill.auth import role
from mill.db import connect, get_db, rows
from mill.inventory import LOW_STOCK
from mill.orders import feasibility, tracking
from mill.purchasing import po_list

router = APIRouter()


CREDIT_DAYS = int(os.environ.get("CREDIT_DAYS", "30"))            # invoice is overdue this long after it is issued

def at_risk_orders(db, today):
    """Cheap rules nominate orders; Jev, asked the same yes/no question as /orders/{id}/risk, confirms.
    An order Jev puts below 50% is dropped unless it is already past its deadline. If Jev is down, the rule stands."""
    free = db.execute("SELECT COUNT(*) FROM machines WHERE status = 'Idle'").fetchone()[0]
    cands = []
    for o in rows(db.execute(
            "SELECT o.*, b.name buyer FROM orders o JOIN buyers b ON b.id = o.buyer_id "
            "WHERE (SELECT COALESCE(SUM(r.metres), 0) FROM rolls r WHERE r.order_id = o.id AND r.status = 'dispatched') < o.qty_m ORDER BY o.deadline")):  # part-shipped orders are still open
        t = tracking(db, o)
        if t["stage"] == "Ready":
            continue
        days_left = (date.fromisoformat(o["deadline"]) - today).days
        short = feasibility(db, o)["short_kg"]
        flags = []
        if days_left < 0:
            flags.append("past its deadline")
        if t["expected_ready"] and t["expected_ready"] > o["deadline"]:
            flags.append(f"at the current pace it will be ready {t['expected_ready']}, after the deadline")
        if not t["expected_ready"] and days_left <= 14:
            flags.append("no output logged in the last 7 days")
        if short > 0 and days_left <= 14:
            flags.append(f"short of {short:g} kg of yarn")
        if flags:
            cands.append({"order_id": o["id"], "buyer": o["buyer"], "fabric": t["fabric"], "deadline": o["deadline"],
                          "days_left": days_left, "progress_pct": t["progress_pct"], "flags": flags,
                          "_state": {"progress_pct": t["progress_pct"], "days_left": days_left, "yarn_short_kg": short,
                                     "free_machines": free}})
    cands = cands[:8]  # a handful of Jev calls at most
    with ThreadPoolExecutor(4) as ex:
        probs = list(ex.map(lambda c: ai.probability(
            c["_state"], "Will this order miss its deadline?",
            "the order is likely to finish after its deadline", "the order will finish on time"), cands))
    out = []
    for c, p in zip(cands, probs):
        c.pop("_state")
        c["risk_probability"] = None if p is None else round(p, 2)
        if p is None or p >= 0.5 or c["days_left"] < 0:
            out.append(c)
    return out


def build_digest(db, today):
    yday = (today - timedelta(days=1)).isoformat()
    made = rows(db.execute("SELECT m.name, m.kind, SUM(p.output) output FROM production_entries p "
                           "JOIN machines m ON m.id = p.machine_id WHERE p.entry_date = ? GROUP BY m.id "
                           "ORDER BY m.kind, m.name", (yday,)))
    low = rows(db.execute(LOW_STOCK))
    drafts = [p for p in po_list(db) if p["status"] == "draft"]
    risky = at_risk_orders(db, today)
    idle = rows(db.execute("SELECT name, status FROM machines WHERE status != 'Running' ORDER BY name"))
    overdue = []
    for i in rows(db.execute(
            "SELECT i.id, i.issued_on, b.name buyer, i.total - COALESCE((SELECT SUM(amount) FROM payments "
            "WHERE invoice_id = i.id), 0) outstanding FROM invoices i JOIN orders o ON o.id = i.order_id "
            "JOIN buyers b ON b.id = o.buyer_id")):
        days_late = (today - date.fromisoformat(i["issued_on"][:10])).days - CREDIT_DAYS
        if i["outstanding"] > 0.005 and days_late > 0:
            overdue.append({"invoice_id": i["id"], "buyer": i["buyer"], "outstanding": round(i["outstanding"], 2),
                            "days_overdue": days_late})
    overdue.sort(key=lambda x: -x["days_overdue"])
    alerts = rows(db.execute("SELECT label, rate, top_defect FROM defect_alerts WHERE status IN ('open', 'acked') "
                             "ORDER BY rate DESC"))
    return {
        "yesterday": {"metres": round(sum(m["output"] for m in made if m["kind"] != "spinning"), 2),
                      "machines_with_output": len(made),
                      "by_machine": [{"machine": m["name"], "output": round(m["output"], 2),
                                      "unit": "kg" if m["kind"] == "spinning" else "m"} for m in made]},
        "low_stock": {"count": len(low), "items": [{"yarn": f"{y['count']} {y['material']} {y['colour']}",
                                                    "stock_kg": round(y["stock_kg"], 2), "reorder_kg": y["reorder_kg"]}
                                                   for y in low]},
        "po_drafts_waiting": {"count": len(drafts), "items": [{"po_id": p["id"], "supplier": p["supplier"],
                                                               "kg": p["total_kg"], "value": p["value"]}
                                                              for p in drafts]},
        "orders_at_risk": {"count": len(risky), "items": risky},
        "machines_not_running": {"count": len(idle), "items": idle},
        "overdue_receivables": {"count": len(overdue), "total": round(sum(o["outstanding"] for o in overdue), 2),
                                "items": overdue[:10]},
        "defect_alerts": {"count": len(alerts), "items": [{"where": a["label"], "failed_pct": round(a["rate"] * 100),
                                                           "main_defect": a["top_defect"]} for a in alerts]},
        "reviews_waiting": db.execute("SELECT COUNT(*) FROM ai_reviews WHERE status = 'open'").fetchone()[0],
    }


def plain_digest(f):
    """The digest with no AI: used when the chat model is unavailable or writes a number that isn't in the facts."""
    y = f["yesterday"]
    lines = [f"Yesterday: {y['metres']:g} m of fabric made." if y["machines_with_output"]
             else "Yesterday: no production was logged."]
    if f["orders_at_risk"]["count"]:
        lines.append("Orders at risk: " + "; ".join(
            f"#{o['order_id']} {o['buyer']} ({o['days_left']} days left, {o['progress_pct']:g}% done)"
            for o in f["orders_at_risk"]["items"]) + ".")
    if f["low_stock"]["count"]:
        lines.append("Low stock: " + ", ".join(f"{i['yarn']} ({i['stock_kg']:g} kg)" for i in f["low_stock"]["items"]) + ".")
    if f["po_drafts_waiting"]["count"]:
        lines.append(f"{f['po_drafts_waiting']['count']} purchase order draft(s) waiting for approval.")
    if f["machines_not_running"]["count"]:
        lines.append("Not running: " + ", ".join(f"{m['name']} ({m['status']})" for m in f["machines_not_running"]["items"]) + ".")
    if f["overdue_receivables"]["count"]:
        lines.append(f"{f['overdue_receivables']['count']} overdue invoice(s), ₹{f['overdue_receivables']['total']:,.0f} outstanding.")
    if f["defect_alerts"]["count"]:
        lines.append("Defect alerts: " + ", ".join(f"{a['where']} ({a['failed_pct']}% failing)" for a in f["defect_alerts"]["items"]) + ".")
    if f["reviews_waiting"]:
        lines.append(f"{f['reviews_waiting']} AI decision(s) waiting for a person to check.")
    return " ".join(lines)


def _numbers(text):
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))}


def numbers_ok(text, facts):
    """Every number the model wrote must already be in the facts. It words the digest; it never does the maths."""
    return _numbers(text) <= _numbers(json.dumps(facts, default=str))


def get_digest(db, today=None, refresh=False):
    today = today or date.today()
    key = today.isoformat()
    row = db.execute("SELECT * FROM digests WHERE digest_date = ?", (key,)).fetchone()
    if not row or refresh:
        facts = build_digest(db, today)
        try:
            summary, source = ai.write_digest(facts), "ai"
            if not summary or not numbers_ok(summary, facts):
                raise ValueError("the summary contains a number that is not in the facts")
        except Exception as e:
            logging.warning("digest written without AI: %r", e)
            summary, source = plain_digest(facts), "template"
        db.execute("INSERT OR REPLACE INTO digests (digest_date, data, summary, source) VALUES (?, ?, ?, ?)",
                   (key, json.dumps(facts), summary, source))
        row = db.execute("SELECT * FROM digests WHERE digest_date = ?", (key,)).fetchone()
    return {"date": row["digest_date"], "summary": row["summary"], "source": row["source"],
            "facts": json.loads(row["data"]), "created_at": row["created_at"]}


@router.get("/digest/today")
def digest_today(db=Depends(get_db), _=role("admin")):
    """Built on first request each day, then kept. Facts are plain code; `summary` is words only (source: ai/template)."""
    return get_digest(db)


@router.post("/digest/refresh")
def digest_refresh(db=Depends(get_db), _=role("admin")):
    return get_digest(db, refresh=True)


def _make_digest():
    db = connect()
    try:
        get_digest(db, refresh=True)
    except Exception:
        logging.exception("scheduled digest failed")
    finally:
        db.close()


async def _digest_loop(hour):
    while True:
        now = datetime.now()
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await asyncio.to_thread(_make_digest)


def start_scheduler():
    """Set DIGEST_HOUR=7 (server local time) to build the digest every morning. Unset = built on first request."""
    hour = os.environ.get("DIGEST_HOUR", "")
    return asyncio.create_task(_digest_loop(int(hour))) if hour.strip() else None
