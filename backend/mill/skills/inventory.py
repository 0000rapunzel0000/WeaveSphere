"""Yarn store and purchasing: receipts, issues, stock, purchase orders."""
from mill.skills import F, STAFF, g, low_line, money, opt, skill

STORE = {"admin", "store"}


def _po_line(db, v, n):
    if not v.get("po"):
        return None
    line = db.execute("SELECT l.id FROM po_lines l WHERE l.po_id = ? AND l.yarn_type_id = ?", (v["po"], v["yarn"])).fetchone()
    if not line:
        return f"PO-{v['po']} has no line for {n['yarn']}. Which PO is it, or none?"
    v["_line"] = line[0]
    return None


skill("receive_yarn", "Record yarn that arrived from a supplier (goods receipt, GRN), optionally against a purchase order.", STORE,
      [F("supplier", "supplier", "supplier"), F("yarn", "yarn", "yarn"), F("kg", "number", "kg"),
       F("rate", "number", "rate per kg (₹)"), F("challan", "text", "challan number", required=False),
       F("po", "po", "purchase order", required=False)],
      lambda v: ("POST", "/grn", {"supplier_id": v["supplier"], "yarn_type_id": v["yarn"], "qty_kg": v["kg"], "rate": v["rate"],
                                  "challan_no": v.get("challan") or None, "po_line_id": v.get("_line")}),
      lambda v, n: f"receive {g(v['kg'])} kg of {n['yarn']} from {n['supplier']} at ₹{g(v['rate'])}/kg"
                   + opt(v.get("challan"), ", challan {}") + opt(v.get("po"), ", against PO-{}"),
      lambda r, v, n: f"Stock is now {g(r['stock']['stock_kg'])} kg." + low_line(r), prepare=_po_line)

skill("issue_yarn", "Issue yarn from the store to an order (optionally to a machine).", STORE,
      [F("order", "order", "order"), F("yarn", "yarn", "yarn"), F("kg", "number", "kg"), F("machine", "machine", "machine", required=False)],
      lambda v: ("POST", "/issues", {"order_id": v["order"], "yarn_type_id": v["yarn"], "qty_kg": v["kg"], "machine_id": v.get("machine")}),
      lambda v, n: f"issue {g(v['kg'])} kg of {n['yarn']} to order #{v['order']}" + opt(n.get("machine"), " on {}"),
      lambda r, v, n: low_line(r).strip())

skill("stock_check", "Say how much yarn is in stock, for one yarn or all of them.", STAFF,
      [F("yarn", "yarn", "yarn", required=False)], lambda v: ("GET", "/stock", None), lambda v, n: "look up stock",
      lambda r, v, n: "\n".join(f"{y['count']} {y['material']} {y['colour']}: {g(y['stock_kg'])} kg" + (" (LOW)" if y["low"] else "")
                                for y in r if not v.get("yarn") or y["id"] == v["yarn"]), read=True)

skill("low_stock", "List the yarns that are at or below their reorder level.", STAFF, [],
      lambda v: ("GET", "/alerts/low-stock", None), lambda v, n: "look up low stock",
      lambda r, v, n: "\n".join(f"{y['count']} {y['material']} {y['colour']}: {g(y['stock_kg'])} kg (reorder at {g(y['reorder_kg'])})" for y in r)
      or "Nothing is low.", read=True)

skill("recent_receipts", "List the latest yarn receipts (GRNs).", STAFF, [], lambda v: ("GET", "/grn", None),
      lambda v, n: "look up receipts",
      lambda r, v, n: "\n".join(f"GRN-{x['id']} {x['received_on']} {x['supplier']}: {g(x['qty_kg'])} kg @ ₹{g(x['rate'])}" for x in r[:10])
      or "No receipts yet.", read=True)


def _po_text(p):
    return (f"PO-{p['id']} {p['supplier']} ({p['status']}): " + "; ".join(
        f"{l['count']} {l['material']} {l['colour']} {g(l['qty_kg'])} kg @ ₹{g(l['rate'])}" for l in p["lines"]) + f", {money(p['value'])}")


skill("purchase_orders", "List purchase orders: the drafts waiting for approval, or approved/received ones.", STAFF,
      [F("status", "choice", "which ones", required=False, options=("draft", "approved", "received", "cancelled"))],
      lambda v: ("GET", "/purchase-orders" + opt(v.get("status"), "?status={}"), None), lambda v, n: "look up purchase orders",
      lambda r, v, n: "\n".join(_po_text(p) for p in r[:10]) or "None.", read=True)

skill("approve_po", "Approve a draft purchase order (it has been placed with the supplier).", {"admin"},
      [F("po", "po", "purchase order")], lambda v: ("POST", f"/purchase-orders/{v['po']}/approve", None),
      lambda v, n: f"approve PO-{v['po']}", lambda r, v, n: "Book the delivery against it when it arrives.")

skill("cancel_po", "Cancel a purchase order (not once yarn has arrived against it).", {"admin"},
      [F("po", "po", "purchase order")], lambda v: ("POST", f"/purchase-orders/{v['po']}/cancel", None),
      lambda v, n: f"cancel PO-{v['po']}")

skill("change_po_quantity", "Change the kg on a draft purchase order line.", {"admin"},
      [F("po", "po", "purchase order"), F("yarn", "yarn", "yarn"), F("kg", "number", "new kg")],
      lambda v: ("PUT", f"/po-lines/{v['_line']}", {"qty_kg": v["kg"]}),
      lambda v, n: f"change {n['yarn']} on PO-{v['po']} to {g(v['kg'])} kg", prepare=_po_line)

skill("raise_po", "Raise a purchase order by hand to a supplier for a yarn.", {"admin"},
      [F("supplier", "supplier", "supplier"), F("yarn", "yarn", "yarn"), F("kg", "number", "kg"), F("rate", "number", "rate per kg (₹)")],
      lambda v: ("POST", "/purchase-orders", {"supplier_id": v["supplier"], "lines": [{"yarn_type_id": v["yarn"], "qty_kg": v["kg"], "rate": v["rate"]}]}),
      lambda v, n: f"raise a PO to {n['supplier']} for {g(v['kg'])} kg of {n['yarn']} at ₹{g(v['rate'])}/kg",
      lambda r, v, n: f"It is PO-{r['po']['id']}, a draft until approved.", gives=("po", lambda r: r["po"]["id"]))

skill("recompute_po_drafts", "Recompute the system's purchase-order drafts from today's yarn shortfalls.", STORE, [],
      lambda v: ("POST", "/purchase-orders/refresh", None), lambda v, n: "recompute the PO drafts",
      lambda r, v, n: f"{len(r['drafts'])} draft PO(s) now waiting." + opt("; ".join(s["why"] for s in r.get("skipped", [])), " {}"), read=True)
