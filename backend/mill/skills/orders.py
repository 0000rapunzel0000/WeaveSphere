"""Buyer orders: booking, status, yarn check, deadline risk, the buyer's tracking link."""
import os

from mill.skills import F, STAFF, g, opt, skill


def _status(o):
    return (f"Order #{o['id']} ({o['buyer']}, {o['fabric']} {o['colour']}): {o['stage']}. {g(o['produced_m'])} of {g(o['qty_m'])} m made "
            f"({g(o['progress_pct'])}%), {g(o['passed_m'])} m passed QC, {g(o['dispatched_m'])} m shipped. Deadline {o['deadline']}"
            + opt(o.get("expected_ready"), ", expected ready {}") + ".")


skill("create_order", "Book a new buyer order: buyer, fabric, colour, metres, deadline and price per metre.", {"admin"},
      [F("buyer", "buyer", "buyer"), F("fabric", "fabric", "fabric"), F("colour", "text", "colour"), F("metres", "number", "metres"),
       F("deadline", "date", "deadline"), F("price", "number", "price per metre (₹)")],
      lambda v: ("POST", "/orders", {"buyer_id": v["buyer"], "fabric_type_id": v["fabric"], "colour": v["colour"], "qty_m": v["metres"],
                                     "deadline": v["deadline"], "price_per_m": v["price"]}),
      lambda v, n: f"book {g(v['metres'])} m of {n['fabric']} in {v['colour']} for {n['buyer']} at ₹{g(v['price'])}/m, due {v['deadline']}",
      lambda r, v, n: f"It is order #{r['id']}; yarn check: {r['feasibility']['verdict']}.")

skill("order_status", "Say where an order is: stage, metres made, passed and shipped, expected ready date, deadline.", STAFF,
      [F("order", "order", "order")], lambda v: ("GET", f"/orders/{v['order']}", None), lambda v, n: f"look up order #{v['order']}",
      lambda r, v, n: _status(r), read=True)

skill("order_list", "List all orders (optionally one buyer's) with their stage and progress.", STAFF,
      [F("buyer", "buyer", "buyer", required=False)], lambda v: ("GET", "/orders" + opt(v.get("buyer"), "?buyer_id={}"), None),
      lambda v, n: "list orders",
      lambda r, v, n: "\n".join(f"#{o['id']} {o['buyer']} {o['fabric']} {o['colour']}: {o['stage']}, {g(o['progress_pct'])}%, due {o['deadline']}"
                                for o in r) or "No orders.", read=True)

skill("yarn_check", "Check whether there is enough yarn for an order (shortfall per yarn).", STAFF,
      [F("order", "order", "order")], lambda v: ("GET", f"/orders/{v['order']}/feasibility", None), lambda v, n: f"check yarn for order #{v['order']}",
      lambda r, v, n: f"Order #{v['order']}: {r['verdict']}. " + "; ".join(
          f"{x['count']} {x['material']}: need {g(x['required_kg'])} kg, issued {g(x['issued_kg'])}, free {g(x['free_kg'])}, short {g(x['short_kg'])}"
          for x in r["lines"]), read=True)

skill("order_risk", "Will an order be LATE: the chance it misses its deadline (deadline risk). Questions like 'will order 2 be late?'.", STAFF,
      [F("order", "order", "order")], lambda v: ("GET", f"/orders/{v['order']}/risk", None), lambda v, n: f"check order #{v['order']}'s deadline risk",
      lambda r, v, n: (f"Order #{v['order']}: " + ("risk unknown (AI unavailable)" if r["probability"] is None else
                       f"{'AT RISK' if r['at_risk'] else 'on track'}, {r['probability']:.0%} chance of missing the deadline")
                       + f". {g(r['progress_pct'])}% done, {r['days_left']} days left, yarn short {g(r['yarn_short_kg'])} kg."), read=True)

skill("tracking_link", "Give the buyer's tracking link for an order (to send to the buyer).", STAFF,
      [F("order", "order", "order")], lambda v: ("GET", f"/orders/{v['order']}", None), lambda v, n: f"get order #{v['order']}'s link",
      lambda r, v, n: f"Order #{r['id']} for {r['buyer']}: {os.environ.get('PUBLIC_URL', 'http://localhost:5173')}/track.html#track={r['token']}",
      read=True)
