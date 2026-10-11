"""Accounts and reports: payments, receivables, payables, P&L, order cost, machine report, the morning digest."""
from mill.skills import F, g, money, opt, skill

OFFICE = {"admin"}


def _invoice_buyer(db, v, n):
    b = db.execute("SELECT o.buyer_id, b.name FROM invoices i JOIN orders o ON o.id = i.order_id JOIN buyers b ON b.id = o.buyer_id "
                   "WHERE i.id = ?", (v["invoice"],)).fetchone()
    v["_buyer"], n["invoice_buyer"] = b[0], b[1]


skill("record_payment", "Record money received from a buyer against one of their invoices.", OFFICE,
      [F("invoice", "invoice", "invoice"), F("amount", "number", "amount (₹)"), F("reference", "text", "bank reference", required=False),
       F("date", "date", "date paid", required=False)],
      lambda v: ("POST", "/payments", {"party": "buyer", "party_id": v["_buyer"], "invoice_id": v["invoice"], "amount": v["amount"],
                                       "ref": v.get("reference") or None, **({"paid_on": v["date"]} if v.get("date") else {})}),
      lambda v, n: f"record {money(v['amount'])} from {n['invoice_buyer']} against INV-{v['invoice']:05d}" + opt(v.get("reference"), " (ref {})"),
      prepare=_invoice_buyer)

skill("pay_supplier", "Record a payment made to a yarn supplier.", OFFICE,
      [F("supplier", "supplier", "supplier"), F("amount", "number", "amount (₹)"), F("reference", "text", "bank reference", required=False)],
      lambda v: ("POST", "/payments", {"party": "supplier", "party_id": v["supplier"], "amount": v["amount"], "ref": v.get("reference") or None}),
      lambda v, n: f"record {money(v['amount'])} paid to {n['supplier']}" + opt(v.get("reference"), " (ref {})"))

skill("receivables", "List what buyers still owe, per invoice.", OFFICE, [], lambda v: ("GET", "/reports/receivables", None),
      lambda v, n: "look up receivables",
      lambda r, v, n: "\n".join(f"INV-{i['id']:05d} {i['buyer']} ({i['issued_on']}): {money(i['outstanding'])} of {money(i['total'])} due"
                                for i in r if i["outstanding"] > 0.005) or "Nothing is owed.", read=True)

skill("payables", "List what the mill owes each supplier.", OFFICE, [], lambda v: ("GET", "/reports/payables", None),
      lambda v, n: "look up payables",
      lambda r, v, n: "\n".join(f"{s['name']}: {money(s['outstanding'])} (billed {money(s['billed'])}, paid {money(s['paid'])})" for s in r), read=True)

skill("profit_and_loss", "Show the month's profit and loss: revenue, yarn, wages, overhead (default this month).", set(),
      [F("month", "month", "month", required=False)], lambda v: ("GET", "/reports/pnl" + opt(v.get("month"), "?month={}"), None),
      lambda v, n: "look up the P&L",
      lambda r, v, n: f"P&L {r['month']}: revenue {money(r['revenue'])}, yarn {money(r['yarn'])}, wages {money(r['wages'])}, "
                      f"overhead {money(r['overhead'])}, profit {money(r['profit'])}.", read=True)

skill("order_cost", "Show what an order cost (yarn, labour, machine overhead) against what it was invoiced, and the margin.", set(),
      [F("order", "order", "order")], lambda v: ("GET", f"/reports/order-cost/{v['order']}", None), lambda v, n: f"cost order #{v['order']}",
      lambda r, v, n: f"Order #{r['order_id']}: yarn {money(r['yarn'])}, wages {money(r['wages'])}, overhead {money(r['overhead'])}, "
                      f"cost {money(r['total_cost'])}, invoiced {money(r['revenue'])}, margin {money(r['margin'])}."
                      + (f" {g(r['made_m'])} m made, so {money(r['cost_per_m'])} per metre." if r["cost_per_m"] else ""), read=True)

skill("machine_report", "Show each machine's output, hours and breakdowns for a month.", set(),
      [F("month", "month", "month", required=False)], lambda v: ("GET", "/reports/machines" + opt(v.get("month"), "?month={}"), None),
      lambda v, n: "look up the machine report",
      lambda r, v, n: "\n".join(f"{m['name']}: {g(m['output'])} output, {g(m['hours'])} h, {m['breakdowns']} breakdown(s)" for m in r), read=True)

skill("morning_digest", "Give today's summary for the owner: output, low stock, PO drafts, orders at risk, overdue invoices, alerts.", OFFICE,
      [], lambda v: ("GET", "/digest/today", None), lambda v, n: "get today's digest", lambda r, v, n: r["summary"], read=True)
