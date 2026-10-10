"""Defect hotspot alerts: a machine+shift or operator whose QC failure rate stands out."""
import logging
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from mill.auth import role, user
from mill.db import get_db, insert, one, rows, tx

router = APIRouter()


# Tunable without code changes. The defaults are a starting point; watch /alerts/defects for a week and adjust.
DEFECT_DAYS = int(os.environ.get("DEFECT_DAYS", "30"))            # how far back QC checks count
DEFECT_MIN_CHECKS = int(os.environ.get("DEFECT_MIN_CHECKS", "5"))  # too few checks and it is just noise
DEFECT_MULT = float(os.environ.get("DEFECT_MULT", "2"))           # alert at this many times the rest-of-mill rate...
DEFECT_POINTS = float(os.environ.get("DEFECT_POINTS", "0.15"))    # ...and at least this far above it (0.15 = 15 points)
ASSUMED_BASELINE = 0.10   # used when the rest of the mill has too few checks to compare against

CHECKS = """FROM qc_records q JOIN rolls r ON r.id = q.roll_id JOIN machines m ON m.id = r.machine_id
            WHERE q.at >= datetime('now', :since)"""


def _count(db, cond, args):
    r = db.execute(f"SELECT COUNT(*) checks, COUNT(*) FILTER (WHERE q.result != 'pass') defects {CHECKS} AND ({cond})",
                   {**args, "since": f"-{DEFECT_DAYS} day"}).fetchone()
    return {"checks": r["checks"], "defects": r["defects"]}


def _top(db, expr, cond, args):
    r = db.execute(f"SELECT {expr} v, COUNT(*) n {CHECKS} AND ({cond}) AND q.result != 'pass' "
                   "GROUP BY v ORDER BY n DESC, v LIMIT 1", {**args, "since": f"-{DEFECT_DAYS} day"}).fetchone()
    return r["v"] if r else None


NEXT_STEP = {  # by the stage that is failing; plain advice, written once here
    "weaving": "Have a fitter check warp tension, reed and shuttle wear, and hold the next rolls for a second QC look.",
    "dyeing": "Check bath recipe, temperature and rinse before the next batch, and hold the next rolls for a second QC look.",
    "spinning": "Check yarn count, twist and cleaning on the frame, and hold the next rolls for a second QC look.",
}


def _note(scope, label, g, rate, baseline, top_defect, top_stage):
    msg = (f"{label}: {g['defects']} of {g['checks']} QC checks failed ({rate:.0%}) against {baseline:.0%} elsewhere "
           f"in the last {DEFECT_DAYS} days")
    if top_defect:
        msg += f", mostly {top_defect.replace('_', ' ')}"
    msg += ". "
    if scope == "operator":
        return msg + "Check set-up and handling with the operator before the next shift."
    return msg + NEXT_STEP.get(top_stage, "Inspect the machine and hold the next rolls for a second QC look.")


def check_defect_hotspots(db, qc_id):
    """Re-judge the machine+shift and the operator behind one QC record. Returns alerts that were newly opened.
    Every QC check counts, so a roll that is reworked and then passed counts twice."""
    r = db.execute("SELECT r.machine_id, r.shift_id, r.operator_id FROM qc_records q JOIN rolls r ON r.id = q.roll_id "
                   "WHERE q.id = ?", (qc_id,)).fetchone()
    if not r:
        return []
    scopes = [
        ("machine_shift", f"m{r['machine_id']}:s{r['shift_id']}", "r.machine_id = :a AND r.shift_id = :b",
         {"a": r["machine_id"], "b": r["shift_id"]},
         lambda: "{} / {}".format(db.execute("SELECT name FROM machines WHERE id = ?", (r["machine_id"],)).fetchone()[0],
                                  db.execute("SELECT name FROM shifts WHERE id = ?", (r["shift_id"],)).fetchone()[0])),
        ("operator", f"w{r['operator_id']}", "r.operator_id = :a", {"a": r["operator_id"]},
         lambda: db.execute("SELECT name FROM workers WHERE id = ?", (r["operator_id"],)).fetchone()[0]),
    ]
    opened = []
    with tx(db):
        for scope, key, cond, args, label in scopes:
            g, rest = _count(db, cond, args), _count(db, f"NOT ({cond})", args)
            baseline = rest["defects"] / rest["checks"] if rest["checks"] >= DEFECT_MIN_CHECKS else ASSUMED_BASELINE
            rate = g["defects"] / g["checks"] if g["checks"] else 0.0
            hot = g["checks"] >= DEFECT_MIN_CHECKS and rate >= max(DEFECT_MULT * baseline, baseline + DEFECT_POINTS)
            live = db.execute("SELECT id FROM defect_alerts WHERE key = ? AND status IN ('open', 'acked')",
                              (key,)).fetchone()
            if not hot:
                if live:  # it went back to normal: close it so the list only ever shows current problems
                    db.execute("UPDATE defect_alerts SET status = 'cleared', updated_at = datetime('now') WHERE id = ?",
                               (live["id"],))
                continue
            top_defect = _top(db, "COALESCE(q.defect_type, 'unclassified')", cond, args)
            top_stage = _top(db, "COALESCE(q.stage_at_fault, m.kind)", cond, args)
            lbl = label()
            fields = {"checks": g["checks"], "defects": g["defects"], "rate": round(rate, 4),
                      "baseline": round(baseline, 4), "top_defect": top_defect, "top_stage": top_stage,
                      "suggestion": _note(scope, lbl, g, rate, baseline, top_defect, top_stage)}
            if live:
                db.execute(f"UPDATE defect_alerts SET {', '.join(k + ' = :' + k for k in fields)}, "
                           "updated_at = datetime('now') WHERE id = :id", {**fields, "id": live["id"]})
            else:
                aid = insert(db, "defect_alerts", {"scope": scope, "key": key, "label": lbl, **fields,
                                                   "machine_id": r["machine_id"] if scope == "machine_shift" else None,
                                                   "shift_id": r["shift_id"] if scope == "machine_shift" else None,
                                                   "worker_id": r["operator_id"] if scope == "operator" else None})
                opened.append(dict(db.execute("SELECT * FROM defect_alerts WHERE id = ?", (aid,)).fetchone()))
    return opened


def after_qc(db, qc_id):
    """Called by app.py after a QC result is saved. A failure here must never undo or block the QC result."""
    try:
        return check_defect_hotspots(db, qc_id)
    except Exception:
        logging.exception("defect hotspot check failed")
        return []


@router.get("/alerts/defects")
def defect_alerts(status: Literal["live", "cleared", "all"] = "live", db=Depends(get_db), _=Depends(user)):
    """live = open + acknowledged. Newest change first."""
    return rows(db.execute(
        "SELECT * FROM defect_alerts WHERE (:s = 'all' OR (:s = 'live' AND status IN ('open', 'acked')) "
        "OR (:s = 'cleared' AND status = 'cleared')) ORDER BY updated_at DESC, id DESC", {"s": status}))


@router.post("/alerts/defects/{aid}/ack")
def ack_defect_alert(aid: int, db=Depends(get_db), u=role("admin", "qc", "supervisor")):
    """'Somebody is on it.' The alert stays listed, and keeps updating, until the numbers return to normal."""
    a = one(db, "SELECT * FROM defect_alerts WHERE id = ?", aid)
    if a["status"] == "cleared":
        raise HTTPException(409, "This alert has already cleared.")
    db.execute("UPDATE defect_alerts SET status = 'acked', acked_by = ? WHERE id = ?", (u["id"], aid))
    return one(db, "SELECT * FROM defect_alerts WHERE id = ?", aid)


# The only GROUP BY expressions the report accepts (never built from user input).
DEFECT_GROUPS = {
    "machine": "m.name", "shift": "s.name", "operator": "w.name", "machine_shift": "m.name || ' / ' || s.name",
}


@router.get("/reports/defects")
def defect_report(group: Literal["machine", "shift", "operator", "machine_shift", "type"] = "machine",
                  days: int = Query(DEFECT_DAYS, ge=1, le=365), db=Depends(get_db), _=role("admin", "qc")):
    """Where defects come from. machine / shift / operator / machine_shift: checks, defects and failure rate, worst first.
    type: how many defects of each type, with share. Answers 'is it Loom 4 on the night shift?'"""
    base = ("FROM qc_records q JOIN rolls r ON r.id = q.roll_id JOIN machines m ON m.id = r.machine_id "
            "JOIN shifts s ON s.id = r.shift_id JOIN workers w ON w.id = r.operator_id "
            "WHERE q.at >= datetime('now', :since)")
    args = {"since": f"-{days} day"}
    total = dict(db.execute(f"SELECT COUNT(*) checks, COUNT(*) FILTER (WHERE q.result != 'pass') defects {base}",
                            args).fetchone())
    total["rate"] = round(total["defects"] / total["checks"], 4) if total["checks"] else None
    if group == "type":
        out = rows(db.execute(f"SELECT COALESCE(q.defect_type, 'unclassified') label, COUNT(*) defects {base} "
                              "AND q.result != 'pass' GROUP BY label ORDER BY defects DESC, label", args))
        for r in out:
            r["share"] = round(r["defects"] / total["defects"], 4) if total["defects"] else None
    else:
        out = rows(db.execute(
            f"SELECT {DEFECT_GROUPS[group]} label, COUNT(*) checks, COUNT(*) FILTER (WHERE q.result != 'pass') defects "
            f"{base} GROUP BY label ORDER BY CAST(defects AS REAL) / COUNT(*) DESC, checks DESC", args))
        for r in out:
            r["rate"] = round(r["defects"] / r["checks"], 4)
    return {"group": group, "days": days, "mill": total, "rows": out}
