"""The live floor engine: running machines turn elapsed time into output and use yarn as they go."""
import logging
import time
from datetime import date, datetime, timezone

from mill.config import ACCRUE_EVERY, SIM_SPEED
from mill.db import connect, insert, rows, tx


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
