"""Dispatch: trucks, loading rolls, closing a truck (raises the GST invoice)."""
from mill.skills import F, STAFF, g, money, skill

STORE = {"admin", "store"}

skill("open_truck", "Open a truck (dispatch) for an order, with the vehicle number and driver.", STORE,
      [F("order", "order", "order"), F("vehicle", "text", "vehicle number"), F("driver", "text", "driver")],
      lambda v: ("POST", "/dispatches", {"order_id": v["order"], "vehicle_no": v["vehicle"], "driver": v["driver"]}),
      lambda v, n: f"open a truck for order #{v['order']}: {v['vehicle']}, driver {v['driver']}",
      lambda r, v, n: f"It is truck #{r['id']}. Load passed rolls onto it.", gives=("truck", lambda r: r["id"]))

skill("load_roll", "Load (scan) a QC-passed roll onto an open truck.", STORE,
      [F("truck", "truck", "truck"), F("roll", "roll", "roll code (ETM-…)")],
      lambda v: ("POST", f"/dispatches/{v['truck']}/scan", {"qr": v["roll"]}),
      lambda v, n: f"load roll {v['roll']} onto {n['truck']}",
      lambda r, v, n: f"Truck #{r['id']} has {r['rolls']} roll(s), {g(r['metres'])} m.")

skill("close_truck", "Close a loaded truck: its rolls are dispatched and the GST invoice is raised.", STORE,
      [F("truck", "truck", "truck")], lambda v: ("POST", f"/dispatches/{v['truck']}/close", None),
      lambda v, n: f"close {n['truck']} and raise the invoice",
      lambda r, v, n: f"Invoice INV-{r['invoice']['id']:05d}: {g(r['invoice']['metres'])} m, {money(r['invoice']['total'])} with GST.",
      gives=("invoice", lambda r: r["invoice"]["id"]))

skill("trucks", "List trucks: the open ones being loaded and recent dispatches.", STAFF, [],
      lambda v: ("GET", "/dispatches", None), lambda v, n: "look up trucks",
      lambda r, v, n: "\n".join(f"#{d['id']} {d['vehicle_no']} · order #{d['order_id']} {d['buyer']} · {d['status']}"
                                + (f" · {money(d['total'])}" if d.get("total") else "") for d in r[:10]) or "No trucks yet.", read=True)

skill("finished_goods", "List finished, QC-passed rolls waiting to be dispatched, per order.", STAFF, [],
      lambda v: ("GET", "/finished-goods", None), lambda v, n: "look up finished goods",
      lambda r, v, n: "\n".join(f"Order #{x['order_id']} {x['buyer']}: {x['rolls']} roll(s), {g(x['metres'])} m" for x in r)
      or "Nothing is waiting.", read=True)
