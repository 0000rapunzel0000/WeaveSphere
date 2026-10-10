"""Payments, order costing, machine utilisation, receivables, payables and monthly P&L."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mill.auth import role
from mill.db import get_db, insert, month_of, one, rows
from mill.workforce import wages

router = APIRouter()


class PaymentIn(BaseModel):
    party: Literal["buyer", "supplier"]
    party_id: int
    amount: float = Field(gt=0)
    invoice_id: int | None = None
    paid_on: date = Field(default_factory=date.today)
    ref: str | None = None


@router.post("/payments")
def record_payment(body: PaymentIn, db=Depends(get_db), _=role("admin")):
    if not db.execute(f"SELECT 1 FROM {body.party}s WHERE id = ?", (body.party_id,)).fetchone():  # party is a Literal
        raise HTTPException(422, f"no {body.party} with id {body.party_id}")
    if body.party == "buyer" and not body.invoice_id:
        raise HTTPException(422, "pick the invoice this payment is for; receivables are tracked per invoice")
    if body.invoice_id:
        inv = one(db, "SELECT o.buyer_id, i.total - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = i.id), 0) "
                      "due FROM invoices i JOIN orders o ON o.id = i.order_id WHERE i.id = ?", body.invoice_id)
        if body.party != "buyer" or inv["buyer_id"] != body.party_id:
            raise HTTPException(422, "invoice does not belong to this buyer")
        if body.amount - inv["due"] > 0.005:
            raise HTTPException(422, f"only ₹{inv['due']:,.2f} is still due on this invoice")
    return one(db, "SELECT * FROM payments WHERE id = ?", insert(db, "payments", body.model_dump(mode="json")))


def order_cost(db, oid):
    yarn = db.execute("SELECT COALESCE(SUM(qty_kg * COALESCE(rate, 0)), 0) FROM material_issues WHERE order_id = ?",
                      (oid,)).fetchone()[0]
    # Piece workers: output x rate. Daily workers: day rate split by their hours on each order that day.
    labour = db.execute("""SELECT COALESCE(SUM(CASE w.wage_type WHEN 'piece' THEN p.output * w.rate
          ELSE w.rate * p.hours / NULLIF((SELECT SUM(q.hours) FROM production_entries q
            WHERE q.operator_id = p.operator_id AND q.entry_date = p.entry_date), 0) END), 0)
        FROM production_entries p JOIN workers w ON w.id = p.operator_id WHERE p.order_id = ?""", (oid,)).fetchone()[0]
    overhead = db.execute("SELECT COALESCE(SUM(p.hours * m.overhead_per_hour), 0) FROM production_entries p "
                          "JOIN machines m ON m.id = p.machine_id WHERE p.order_id = ?", (oid,)).fetchone()[0]
    revenue = db.execute("SELECT COALESCE(SUM(subtotal), 0) FROM invoices WHERE order_id = ?", (oid,)).fetchone()[0]
    cost = yarn + labour + overhead
    return {"order_id": oid, "yarn": round(yarn, 2), "wages": round(labour, 2), "overhead": round(overhead, 2),
            "total_cost": round(cost, 2), "revenue": round(revenue, 2), "margin": round(revenue - cost, 2)}


@router.get("/reports/order-cost")
def order_costs(db=Depends(get_db), _=role()):
    return [order_cost(db, r[0]) for r in db.execute("SELECT id FROM orders ORDER BY id DESC")]


@router.get("/reports/order-cost/{oid}")
def order_cost_one(oid: int, db=Depends(get_db), _=role()):
    one(db, "SELECT id FROM orders WHERE id = ?", oid)
    return order_cost(db, oid)


@router.get("/reports/machines")
def machine_output(month: str | None = None, db=Depends(get_db), _=role()):
    return rows(db.execute("""SELECT m.id, m.name, m.kind,
          COALESCE(SUM(p.output), 0) output, COALESCE(SUM(p.hours), 0) hours,
          (SELECT COUNT(*) FROM machine_status_log l WHERE l.machine_id = m.id AND l.status = 'Breakdown'
            AND strftime('%Y-%m', l.at) = :m) breakdowns
        FROM machines m LEFT JOIN production_entries p ON p.machine_id = m.id AND strftime('%Y-%m', p.entry_date) = :m
        GROUP BY m.id ORDER BY m.kind, m.name""", {"m": month_of(month)}))


@router.get("/reports/receivables")
def receivables(db=Depends(get_db), _=role("admin")):
    return rows(db.execute("""SELECT i.*, b.id buyer_id, b.name buyer,
          COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = i.id), 0) paid,
          i.total - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = i.id), 0) outstanding
        FROM invoices i JOIN orders o ON o.id = i.order_id JOIN buyers b ON b.id = o.buyer_id ORDER BY i.id"""))


@router.get("/reports/payables")
def payables(db=Depends(get_db), _=role("admin")):
    """Supplier bills are the GRNs (qty x rate)."""
    return rows(db.execute("""SELECT s.id, s.name,
          COALESCE((SELECT SUM(qty_kg * rate) FROM grn WHERE supplier_id = s.id), 0) billed,
          COALESCE((SELECT SUM(amount) FROM payments WHERE party = 'supplier' AND party_id = s.id), 0) paid,
          COALESCE((SELECT SUM(qty_kg * rate) FROM grn WHERE supplier_id = s.id), 0)
            - COALESCE((SELECT SUM(amount) FROM payments WHERE party = 'supplier' AND party_id = s.id), 0) outstanding
        FROM suppliers s ORDER BY s.id"""))


@router.get("/reports/pnl")
def pnl(month: str | None = None, db=Depends(get_db), _=role()):
    m = month_of(month)
    q = lambda sql: db.execute(sql, (m,)).fetchone()[0]  # noqa: E731
    revenue = q("SELECT COALESCE(SUM(subtotal), 0) FROM invoices WHERE strftime('%Y-%m', issued_on) = ?")  # ex-GST
    yarn = q("SELECT COALESCE(SUM(qty_kg * COALESCE(rate, 0)), 0) FROM material_issues "
             "WHERE strftime('%Y-%m', issued_on) = ?")
    wage_total = sum(w["wage"] for w in wages(db, m))
    overhead = q("SELECT COALESCE(SUM(p.hours * mc.overhead_per_hour), 0) FROM production_entries p "
                 "JOIN machines mc ON mc.id = p.machine_id WHERE strftime('%Y-%m', p.entry_date) = ?")
    costs = yarn + wage_total + overhead
    return {"month": m, "revenue": round(revenue, 2), "yarn": round(yarn, 2), "wages": round(wage_total, 2),
            "overhead": round(overhead, 2), "total_costs": round(costs, 2), "profit": round(revenue - costs, 2)}
