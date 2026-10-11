"""Production floor: shift output, rolls, machine status, job cards, the live board."""
from mill.skills import F, STAFF, g, opt, skill

FLOOR = {"admin", "supervisor"}

skill("log_production", "Record how much a machine PRODUCED in a shift: a quantity made (e.g. '120 m', '120 meter'), for an order, by an "
      "operator, with the hours worked. Any message that reports an amount woven, dyed or spun is this.", FLOOR,
      [F("machine", "machine", "machine"), F("order", "order", "order"), F("shift", "shift", "shift"),
       F("operator", "worker", "operator"), F("output", "number", "output (metres; kg on spinning)"),
       F("hours", "number", "hours worked"), F("date", "date", "date", required=False)],
      lambda v: ("POST", "/production", {"machine_id": v["machine"], "order_id": v["order"], "shift_id": v["shift"],
                                         "operator_id": v["operator"], "output": v["output"], "hours": v["hours"],
                                         **({"entry_date": v["date"]} if v.get("date") else {})}),
      lambda v, n: f"log {g(v['output'])} m on {n['machine']} for order #{v['order']}, {n['shift']} shift, {n['operator']}, "
                   f"{g(v['hours'])} h" + opt(v.get("date"), ", on {}"))

skill("make_roll", "Create a finished roll of fabric off a machine; it gets a QR label and goes to the QC queue.", FLOOR,
      [F("order", "order", "order"), F("machine", "machine", "machine"), F("shift", "shift", "shift"),
       F("operator", "worker", "operator"), F("metres", "number", "metres in the roll")],
      lambda v: ("POST", "/rolls", {"order_id": v["order"], "machine_id": v["machine"], "shift_id": v["shift"],
                                    "operator_id": v["operator"], "metres": v["metres"]}),
      lambda v, n: f"make a {g(v['metres'])} m roll for order #{v['order']} from {n['machine']}, {n['shift']} shift, {n['operator']}",
      lambda r, v, n: f"Roll {r['qr']} is waiting for QC.", gives=("roll", lambda r: r["qr"]),
      needs=r"\brolls?\b|ரோல்|रोल|ರೋಲ್")  # "loom 4 did 120 m" is output to log, never a roll


def _triage(r, v, n):
    t = r.get("triage")
    if not t:
        return ""
    s = t["suggestion"]
    return (f"Jev: {s.get('category', '').replace('_', ' ')}, {s.get('severity')} severity, send the {s.get('assignee')}."
            if t["auto"] else "The breakdown is in AI review for the floor team to classify.")


skill("machine_status", "CHANGE a machine's STATUS: set it to running / idle / breakdown / maintenance, or report it started, stopped, "
      "broke down (and what happened) or went for maintenance. Any 'set … status to …' is this. A statement, not a question.", FLOOR,
      [F("machine", "machine", "machine"), F("status", "choice", "status", options=("Running", "Idle", "Breakdown", "Maintenance")),
       F("note", "text", "what happened", required=False)],
      lambda v: ("POST", f"/machines/{v['machine']}/status", {"status": v["status"], "note": v.get("note") or None}),
      lambda v, n: f"set {n['machine']} to {v['status']}" + opt(v.get("note"), " ({})"), _triage,
      rules=[lambda v: "What happened to the machine?" if v.get("status") == "Breakdown" and not v.get("note") else None])

skill("assign_job", "PUT a new order on a machine (open a job card): which order a machine should run next, and who runs it. No "
      "amount produced and no status change (starting/stopping the machine) is mentioned.", FLOOR,
      [F("order", "order", "order"), F("machine", "machine", "machine"), F("operator", "worker", "operator"),
       F("shift", "shift", "shift")],
      lambda v: ("POST", "/jobs", {"order_id": v["order"], "machine_id": v["machine"], "operator_id": v["operator"],
                                   "shift_id": v["shift"]}),
      lambda v, n: f"put order #{v['order']} on {n['machine']} with {n['operator']}, {n['shift']} shift")

def _current_job(db, v, n):
    """The machine's current job gives the order and shift unless the person named them. No job and no order: ask."""
    j = db.execute("SELECT j.order_id, j.shift_id, w.name, s.name FROM job_cards j LEFT JOIN workers w ON w.id = j.operator_id "
                   "LEFT JOIN shifts s ON s.id = j.shift_id WHERE j.machine_id = ? AND j.status = 'open'", (v["machine"],)).fetchone()
    n["_was"] = (j[2] if j else None)
    v["_order"] = v.get("order") or (j and j[0])
    if not v["_order"]:
        return f"Which order should {n['machine']} run?"
    if not v.get("shift"):
        if not (j and j[1]):
            return "Which shift?"
        v["shift"], n["shift"] = j[1], j[3]


skill("change_operator", "PUT a worker on a machine: on its current job ('Arun instead of Lakshmi on loom 2', 'put Meena on "
      "loom 4'), or on a new order if one is named.", FLOOR,
      [F("machine", "machine", "machine"), F("operator", "worker", "operator"), F("shift", "shift", "shift", required=False),
       F("order", "order", "order, if it changes", required=False)],
      lambda v: ("POST", "/jobs", {"order_id": v["_order"], "machine_id": v["machine"], "operator_id": v["operator"], "shift_id": v["shift"]}),
      lambda v, n: f"put {n['operator']} on {n['machine']} (order #{v.get('_order', '?')}, {n.get('shift', '?')} shift)"
                   + (f" instead of {n['_was']}" if n.get("_was") and n["_was"] != n["operator"] else ""),
      prepare=_current_job)


def _other_jobs(db, v, n):
    # The list is fixed now, so a job a later step gives them is never freed; in a plan, the machine another step puts
    # them on is taken off it too (assistant.other_jobs_only).
    jobs = db.execute("SELECT j.id, m.name, j.order_id, m.status, j.machine_id FROM job_cards j JOIN machines m ON m.id = j.machine_id "
                      "WHERE j.status = 'open' AND j.operator_id = ?", (v["worker"],)).fetchall()
    v["_jobs"], v["_job_machines"] = [j[0] for j in jobs], [j[4] for j in jobs]
    n["_job_list"] = [f"{j[1]} (order #{j[2]}{', it stops until someone else is put on it' if j[3] == 'Running' else ''})" for j in jobs]
    n["_jobs"] = "; ".join(n["_job_list"])


skill("release_worker", "FREE a worker from the jobs they are on now (take them off their machines), e.g. to move them to "
      "another machine. Those machines stop until someone else is put on them.", FLOOR,
      [F("worker", "worker", "worker")],
      lambda v: ("POST", f"/workers/{v['worker']}/release", {"job_ids": v["_jobs"]}),
      lambda v, n: (f"take {n['worker']} off {n['_jobs']}" if n.get("_jobs") else f"leave {n['worker']} as is: no other jobs to free them from"),
      lambda r, v, n: "", prepare=_other_jobs)

skill("finish_job", "Mark the job on a machine as done (close its job card).", FLOOR,
      [F("job", "job", "machine whose job is done")],
      lambda v: ("POST", f"/jobs/{v['job']}/done", None),
      lambda v, n: f"close the job {n['job']}")

skill("floor_status", "LOOK UP the machines right now: a question like 'is loom 4 running?' or 'what is each machine doing?' (status, order, metres done).", STAFF,
      [], lambda v: ("GET", "/floor", None), lambda v, n: "look at the floor",
      lambda r, v, n: "\n".join(
          f"{m['name']}: {m['status']}" + (f", order #{m['order_id']} {g(m['stage_done_m'])}/{g(m['qty_m'])} m" if m["order_id"] else ", no job")
          + opt(m.get("operator"), ", {}") for m in r["machines"]), read=True)

skill("machine_history", "Show a machine's recent status changes and breakdown notes.", STAFF,
      [F("machine", "machine", "machine")], lambda v: ("GET", f"/machines/{v['machine']}/log", None),
      lambda v, n: f"look at {n['machine']}'s history",
      lambda r, v, n: "\n".join(f"{x['at'][:16]} {x['status']}" + opt(x.get("note"), ": {}") + opt(x.get("assignee"), " → {}")
                                for x in r[:6]) or "No status changes logged.", read=True)

skill("production_on_day", "Show the production entries logged on a day (default today).", STAFF,
      [F("date", "date", "date", required=False)],
      lambda v: ("GET", "/production" + opt(v.get("date"), "?entry_date={}"), None),
      lambda v, n: "look up production",
      lambda r, v, n: "\n".join([f"{x['entry_date']} {x['machine']} {x['shift']}: {g(x['output'])} for order #{x['order_id']} by {x['operator']}"
                                 for x in r][:15]) or "Nothing logged.", read=True)
