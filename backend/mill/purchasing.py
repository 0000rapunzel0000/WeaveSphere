"""Purchase-order drafts from yarn shortfalls, approval, and receiving against PO lines."""
import logging
import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mill.auth import role, user
from mill.db import get_db, insert, one, rows, tx

router = APIRouter()


# Per yarn type: what open orders still need + the reorder cushion, against stock and yarn already on order.
# Lines on auto-drafted, unedited POs are left out of "on order" because refresh recomputes exactly those.
NEEDS_SQL = """
SELECT y.id yarn_type_id, y.count, y.material, y.colour, y.reorder_kg, y.stock_kg,
  COALESCE((SELECT SUM(MAX(0, o.qty_m * b.kg_per_m - COALESCE((SELECT SUM(i.qty_kg) FROM material_issues i
                                                               WHERE i.order_id = o.id AND i.yarn_type_id = y.id), 0)))
            FROM orders o JOIN bom b ON b.fabric_type_id = o.fabric_type_id AND b.yarn_type_id = y.id
            WHERE (SELECT COALESCE(SUM(r.metres), 0) FROM rolls r WHERE r.order_id = o.id AND r.status = 'dispatched') < o.qty_m), 0) demand_kg,
  COALESCE((SELECT SUM(MAX(0, l.qty_kg - COALESCE((SELECT SUM(g.qty_kg) FROM po_receipts rc JOIN grn g ON g.id = rc.grn_id
                                                    WHERE rc.po_line_id = l.id), 0)))
            FROM po_lines l JOIN purchase_orders p ON p.id = l.po_id
            WHERE l.yarn_type_id = y.id AND p.status != 'cancelled'
              AND NOT (p.status = 'draft' AND p.auto = 1 AND l.locked = 0)), 0) on_order_kg
FROM yarn_stock y"""


def yarn_needs(db):
    out = rows(db.execute(NEEDS_SQL))
    for n in out:
        n["need_kg"] = n["demand_kg"] + n["reorder_kg"] - n["stock_kg"] - n["on_order_kg"]
    return out


def pick_supplier(db, yarn_type_id):
    """The supplier whose latest rate for this yarn is lowest, looking at the last few GRNs; newest wins a tie.
    # ponytail: no delivery-date data exists yet, so on-time history isn't part of the choice."""
    seen = {}
    for g in db.execute("SELECT supplier_id, rate FROM grn WHERE yarn_type_id = ? ORDER BY id DESC LIMIT 10",
                        (yarn_type_id,)):
        seen.setdefault(g["supplier_id"], g["rate"])  # first row per supplier is its latest
    if not seen:
        return None
    sup = min(seen, key=lambda s: seen[s])  # min keeps the first (most recently seen) on a tie
    return sup, seen[sup]


def refresh_po_drafts(db):
    """Make the system's draft POs match today's need. People's work is never touched: approved POs, manual POs and
    lines a person edited all count as 'on order' and are left alone."""
    out = {"drafted": [], "skipped": []}
    with tx(db):
        existing = {r["yarn_type_id"]: dict(r) for r in db.execute(
            "SELECT l.*, p.supplier_id FROM po_lines l JOIN purchase_orders p ON p.id = l.po_id "
            "WHERE p.status = 'draft' AND p.auto = 1 AND l.locked = 0")}
        for n in yarn_needs(db):
            cur = existing.get(n["yarn_type_id"])
            need = math.ceil(n["need_kg"] - 1e-9) if n["need_kg"] > 1e-9 else 0
            pick = pick_supplier(db, n["yarn_type_id"]) if need else None
            if not need or not pick:
                if cur:
                    db.execute("DELETE FROM po_lines WHERE id = ?", (cur["id"],))
                if need:
                    out["skipped"].append({"yarn_type_id": n["yarn_type_id"], "need_kg": need,
                                           "why": "no supplier history for this yarn yet; raise a PO by hand"})
                continue
            sup, rate = pick
            kg = lambda v: f"{round(v, 1):g}"  # noqa: E731  live looms leave long decimals
            reason = (f"open orders still need {kg(n['demand_kg'])} kg, stock is {kg(n['stock_kg'])} kg, "
                      f"{kg(n['on_order_kg'])} kg already on order, keep {kg(n['reorder_kg'])} kg in reserve")
            if cur and cur["supplier_id"] == sup:
                db.execute("UPDATE po_lines SET qty_kg = ?, rate = ?, reason = ? WHERE id = ?",
                           (need, rate, reason, cur["id"]))
                line_id = cur["id"]
            else:
                if cur:
                    db.execute("DELETE FROM po_lines WHERE id = ?", (cur["id"],))
                # Not a draft that already holds a person-edited line for this yarn (one line per yarn per PO).
                po = db.execute("SELECT id FROM purchase_orders p WHERE supplier_id = ? AND status = 'draft' AND auto = 1 "
                                "AND NOT EXISTS (SELECT 1 FROM po_lines WHERE po_id = p.id AND yarn_type_id = ?)",
                                (sup, n["yarn_type_id"])).fetchone()
                po_id = po["id"] if po else insert(db, "purchase_orders", {"supplier_id": sup, "auto": 1,
                                                                            "note": "Drafted from yarn shortfall"})
                line_id = insert(db, "po_lines", {"po_id": po_id, "yarn_type_id": n["yarn_type_id"], "qty_kg": need,
                                                  "rate": rate, "reason": reason})
            out["drafted"].append({"line_id": line_id, "yarn_type_id": n["yarn_type_id"], "supplier_id": sup,
                                   "qty_kg": need, "rate": rate})
        db.execute("DELETE FROM purchase_orders WHERE auto = 1 AND status = 'draft' "
                   "AND NOT EXISTS (SELECT 1 FROM po_lines WHERE po_id = purchase_orders.id)")
    return out


def auto_po(db):
    """Called by app.py after an order, GRN or issue changes what is needed or in stock. Never breaks that request."""
    try:
        return refresh_po_drafts(db)
    except Exception:
        logging.exception("PO draft refresh failed")
        return None


def check_po_line(db, line_id, supplier_id, yarn_type_id):
    """A GRN may only be booked against an approved PO line for the same supplier and yarn."""
    l = db.execute("SELECT l.yarn_type_id, p.supplier_id, p.status FROM po_lines l "
                   "JOIN purchase_orders p ON p.id = l.po_id WHERE l.id = ?", (line_id,)).fetchone()
    if not l:
        raise HTTPException(404, "unknown PO line")
    if l["status"] != "approved":
        raise HTTPException(409, f"That PO is {l['status']}; approve it before receiving yarn against it.")
    if l["yarn_type_id"] != yarn_type_id or l["supplier_id"] != supplier_id:
        raise HTTPException(409, "This delivery doesn't match the PO line's supplier and yarn.")


def po_list(db, po_id=None):
    pos = rows(db.execute("SELECT p.*, s.name supplier FROM purchase_orders p JOIN suppliers s ON s.id = p.supplier_id "
                          "WHERE :id IS NULL OR p.id = :id ORDER BY p.id DESC", {"id": po_id}))
    for p in pos:
        p["auto"] = bool(p["auto"])
        p["lines"] = rows(db.execute("""SELECT l.*, y.count, y.material, y.colour,
            COALESCE((SELECT SUM(g.qty_kg) FROM po_receipts rc JOIN grn g ON g.id = rc.grn_id
                      WHERE rc.po_line_id = l.id), 0) received_kg
            FROM po_lines l JOIN yarn_types y ON y.id = l.yarn_type_id WHERE l.po_id = ? ORDER BY l.id""", (p["id"],)))
        for l in p["lines"]:
            l["locked"] = bool(l["locked"])
            l["open_kg"] = round(max(0, l["qty_kg"] - l["received_kg"]), 2)
        p["total_kg"] = round(sum(l["qty_kg"] for l in p["lines"]), 2)
        p["value"] = round(sum(l["qty_kg"] * l["rate"] for l in p["lines"]), 2)
        if p["status"] == "approved" and p["lines"] and all(l["open_kg"] == 0 for l in p["lines"]):
            p["status"] = "received"  # derived from GRNs, never stored
    return pos


class PoLineIn(BaseModel):
    yarn_type_id: int
    qty_kg: float = Field(gt=0)
    rate: float = Field(0, ge=0)


class PoIn(BaseModel):
    supplier_id: int
    note: str | None = None
    lines: list[PoLineIn] = Field(min_length=1)


class PoLineEdit(BaseModel):
    qty_kg: float | None = Field(None, gt=0)
    rate: float | None = Field(None, ge=0)


@router.get("/purchase-orders")
def list_purchase_orders(status: Literal["draft", "approved", "received", "cancelled"] | None = None,
                         db=Depends(get_db), _=Depends(user)):
    return [p for p in po_list(db) if status is None or p["status"] == status]


@router.get("/purchase-orders/{po_id}")
def get_purchase_order(po_id: int, db=Depends(get_db), _=Depends(user)):
    one(db, "SELECT id FROM purchase_orders WHERE id = ?", po_id)
    return po_list(db, po_id)[0]


@router.post("/purchase-orders")
def create_purchase_order(body: PoIn, db=Depends(get_db), _=role("admin")):
    """A PO raised by hand. It is a draft until approved, and counts as 'on order' straight away."""
    with tx(db):
        pid = insert(db, "purchase_orders", {"supplier_id": body.supplier_id, "note": body.note})
        for l in body.lines:
            insert(db, "po_lines", {"po_id": pid, **l.model_dump(), "locked": 1})
    return {"po": po_list(db, pid)[0], "po_refresh": auto_po(db)}


@router.post("/purchase-orders/refresh")
def refresh_purchase_orders(db=Depends(get_db), _=role("admin", "store")):
    """Recompute the system's draft POs now (this also happens after every order, GRN and issue)."""
    return {**refresh_po_drafts(db), "drafts": [p for p in po_list(db) if p["status"] == "draft"]}


@router.put("/po-lines/{line_id}")
def edit_po_line(line_id: int, body: PoLineEdit, db=Depends(get_db), _=role("admin")):
    """Change a draft line's quantity or rate. The system stops recomputing a line a person has edited."""
    l = one(db, "SELECT l.id, l.po_id, p.status FROM po_lines l JOIN purchase_orders p ON p.id = l.po_id WHERE l.id = ?",
            line_id)
    if l["status"] != "draft":
        raise HTTPException(409, f"Only a draft PO can be edited; this one is {l['status']}.")
    changes = body.model_dump(exclude_none=True)
    if changes:
        db.execute(f"UPDATE po_lines SET {', '.join(k + ' = :' + k for k in changes)}, locked = 1 WHERE id = :id",
                   {**changes, "id": line_id})
        auto_po(db)  # what the person no longer covers is drafted now, not after the next unrelated receipt
    return po_list(db, l["po_id"])[0]


@router.post("/purchase-orders/{po_id}/approve")
def approve_purchase_order(po_id: int, db=Depends(get_db), u=role("admin")):
    """Approve = the order is placed with the supplier. Receive it by booking GRNs with po_line_id."""
    with tx(db):
        p = one(db, "SELECT * FROM purchase_orders WHERE id = ?", po_id)
        if p["status"] != "draft":
            raise HTTPException(409, f"Only a draft can be approved; this PO is {p['status']}.")
        db.execute("UPDATE purchase_orders SET status = 'approved', approved_by = ?, approved_at = datetime('now') "
                   "WHERE id = ?", (u["id"], po_id))
        db.execute("UPDATE po_lines SET locked = 1 WHERE po_id = ?", (po_id,))
    return po_list(db, po_id)[0]


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_purchase_order(po_id: int, db=Depends(get_db), _=role("admin")):
    with tx(db):
        p = one(db, "SELECT * FROM purchase_orders WHERE id = ?", po_id)
        if p["status"] == "cancelled":
            raise HTTPException(409, "This PO is already cancelled.")
        if db.execute("SELECT 1 FROM po_receipts rc JOIN po_lines l ON l.id = rc.po_line_id WHERE l.po_id = ?",
                      (po_id,)).fetchone():
            raise HTTPException(409, "Yarn has already been received against this PO, so it can't be cancelled.")
        db.execute("UPDATE purchase_orders SET status = 'cancelled' WHERE id = ?", (po_id,))
    return {"po": po_list(db, po_id)[0], "po_refresh": auto_po(db)}  # the shortfall is open again: redraft it
