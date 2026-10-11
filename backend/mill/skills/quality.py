"""Quality and traceability: QC results, the queue, defect hotspots, tracing a roll."""
from mill.skills import F, STAFF, g, money, opt, skill

QC = {"admin", "qc"}


def _qc_done(r, v, n):
    c, out = r.get("classification"), ""
    if c:
        out = ("Jev classified it: " + ", ".join(f"{k.replace('_', ' ')} {str(x).replace('_', ' ')}" for k, x in c["suggestion"].items()) + "."
               if c["auto"] else "The defect is in AI review for QC to classify.")
    if r.get("defect_alerts"):
        out += " DEFECT ALERT: " + " ".join(a["suggestion"] for a in r["defect_alerts"])
    return out.strip()


skill("qc_result", "Record a QC inspection of a fabric ROLL (a roll code like ETM-…): it passed / looks fine, needs rework, or is rejected (and what is wrong).", QC,
      [F("roll", "roll", "roll code (ETM-…)"), F("result", "choice", "result", options=("pass", "rework", "reject")),
       F("notes", "text", "what is wrong", required=False)],
      lambda v: ("POST", f"/qc/{v['roll']}", {"result": v["result"], "notes": v.get("notes") or None}),
      lambda v, n: f"mark roll {v['roll']} as {v['result']}" + opt(v.get("notes"), " ({})"), _qc_done,
      rules=[lambda v: "What is wrong with the roll?" if v.get("result") in ("rework", "reject") and not v.get("notes") else None])

skill("qc_queue", "List the rolls waiting for QC inspection (or back from rework).", STAFF, [],
      lambda v: ("GET", "/qc/queue", None), lambda v, n: "look up the QC queue",
      lambda r, v, n: "\n".join(f"{x['qr']} · order #{x['order_id']} · {g(x['metres'])} m · {x['machine']} · {x['status'].replace('_', ' ')}" for x in r)
      or "No rolls are waiting.", read=True)

skill("defect_alerts", "List the live defect hotspot alerts (a machine/shift or operator failing QC far more than the rest).", STAFF, [],
      lambda v: ("GET", "/alerts/defects", None), lambda v, n: "look up defect alerts",
      lambda r, v, n: "\n".join(f"#{a['id']} {a['label']} ({a['status']}): {a['suggestion']}" for a in r) or "No live alerts.", read=True)

skill("acknowledge_alert", "Acknowledge a defect hotspot alert: someone is on it.", {"admin", "qc", "supervisor"},
      [F("alert", "alert", "alert")], lambda v: ("POST", f"/alerts/defects/{v['alert']}/ack", None),
      lambda v, n: f"mark the alert for {n['alert']} as being handled")

skill("defect_report", "Show where defects come from: by machine, shift, operator, machine and shift, or defect type.", QC,
      [F("group", "choice", "grouped by", required=False, options=("machine_shift", "machine", "shift", "operator", "type"))],
      lambda v: ("GET", "/reports/defects?group=" + (v.get("group") or "machine_shift"), None), lambda v, n: "look up the defect report",
      lambda r, v, n: f"Mill: {r['mill']['defects']} of {r['mill']['checks']} checks failed.\n" + "\n".join(
          f"{x['label']}: " + (f"{x['defects']} ({x['share']:.0%})" if "share" in x else f"{x['defects']} of {x['checks']} ({x['rate']:.0%})")
          for x in r["rows"][:10]), read=True)

skill("trace_roll", "Trace a roll: its order, buyer, machine, shift, operator, yarn, QC history and truck.", STAFF,
      [F("roll", "roll", "roll code (ETM-…)")], lambda v: ("GET", f"/rolls/{v['roll']}", None), lambda v, n: f"trace roll {v['roll']}",
      lambda r, v, n: (f"{r['qr']}: {g(r['metres'])} m for order #{r['order_id']} ({r['buyer']}), {r['machine']}, {r['shift']} shift, "
                       f"{r['operator']}, made {r['made_on']}. Status {r['status'].replace('_', ' ')}."
                       + opt("; ".join(f"{q['result']}" + opt(q.get("defect_type"), " ({})") for q in r["qc_history"]), " QC: {}.")
                       + opt(r["dispatch"] and f"truck #{r['dispatch']['id']} {r['dispatch']['vehicle_no']}", " Left on {}.")
                       + (f" It cost {money(r['cost']['roll'])} ({money(r['cost']['per_m'])}/m)." if r.get("cost") else "")), read=True)
