"""Workforce: attendance, the register, wages."""
from mill.skills import F, g, money, opt, skill

FLOOR = {"admin", "supervisor"}

skill("mark_attendance", "RECORD attendance: who came to work, who is present or absent on a shift (writing the attendance register).", FLOOR,
      [F("shift", "shift", "shift"), F("workers", "workers", "workers present"), F("date", "date", "date", required=False)],
      lambda v: ("POST", "/attendance", {"shift_id": v["shift"], "worker_ids": v["workers"], **({"work_date": v["date"]} if v.get("date") else {})}),
      lambda v, n: f"mark {n['workers']} present on the {n['shift']} shift" + opt(v.get("date"), " on {}"))

def _day(db, v, n):
    from datetime import date
    v["date"] = v.get("date") or date.today().isoformat()
    n["_workers"] = {r[0]: r[1] for r in db.execute("SELECT id, name FROM workers")}


def _present(r, v, n):
    on = [x for x in r["records"] if x["work_date"] == v["date"]]
    by = {}
    for x in on:
        by.setdefault(x["shift"], []).append(n["_workers"].get(x["worker_id"], "?"))
    return f"{v['date']}: " + ("; ".join(f"{s}: {', '.join(w)}" for s, w in by.items()) if by else "nobody marked present.")


skill("attendance_on_day", "LOOK UP attendance: a question asking who was present on a day. Only reading the register, not recording it.", FLOOR,
      [F("date", "date", "date", required=False)], lambda v: ("GET", "/attendance?month=" + v["date"][:7], None),
      lambda v, n: "look up attendance", _present, read=True, prepare=_day)

skill("wages", "Show the month's wages per worker (default this month).", {"admin"},
      [F("month", "month", "month", required=False)], lambda v: ("GET", "/wages" + opt(v.get("month"), "?month={}"), None),
      lambda v, n: "look up wages",
      lambda r, v, n: f"Wages {r['month']}: {money(r['total'])}.\n" + "\n".join(
          f"{w['name']}: {money(w['wage'])} ({w['days']} days" + (f", {g(w['output'])} output" if w["wage_type"] == "piece" else "") + ")"
          for w in r["workers"]), read=True)
