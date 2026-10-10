"""Finished goods and dispatch: trucks, roll scanning, and the GST invoice raised on close."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mill.auth import role, user
from mill.db import get_db, insert, one, rows, tx
from mill.production import roll_code

router = APIRouter()


class DispatchIn(BaseModel):
    order_id: int
    vehicle_no: str = Field(min_length=1)
    driver: str = Field(min_length=1)


class ScanIn(BaseModel):
    qr: str


@router.get("/finished-goods")
def finished_goods(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("""SELECT o.id order_id, b.name buyer, f.name fabric, o.colour,
          COUNT(*) rolls, SUM(r.metres) metres
        FROM rolls r JOIN orders o ON o.id = r.order_id JOIN buyers b ON b.id = o.buyer_id
        JOIN fabric_types f ON f.id = o.fabric_type_id
        WHERE r.status = 'passed' GROUP BY o.id ORDER BY o.id"""))


@router.post("/dispatches")
def create_dispatch(body: DispatchIn, db=Depends(get_db), _=role("admin", "store")):
    return one(db, "SELECT * FROM dispatches WHERE id = ?", insert(db, "dispatches", body.model_dump()))


@router.post("/dispatches/{did}/scan")
def scan_roll(did: int, body: ScanIn, db=Depends(get_db), _=role("admin", "store")):
    """Hard guards, no AI: open dispatch, not already dispatched, right order, QC-passed."""
    with tx(db):
        d = one(db, "SELECT * FROM dispatches WHERE id = ?", did)
        if d["status"] != "open":
            raise HTTPException(409, "dispatch is closed")
        r = db.execute("SELECT * FROM rolls WHERE qr = ?", (roll_code(body.qr),)).fetchone()
        if not r:
            raise HTTPException(404, "unknown roll")
        if on := db.execute("SELECT dispatch_id FROM dispatch_items WHERE roll_id = ?", (r["id"],)).fetchone():
            raise HTTPException(409, f"BLOCKED: roll already scanned on dispatch #{on[0]}")
        if r["order_id"] != d["order_id"]:
            raise HTTPException(409, f"BLOCKED: roll belongs to order #{r['order_id']}, not #{d['order_id']}")
        if r["status"] != "passed":
            raise HTTPException(409, f"BLOCKED: roll is {r['status']}, not QC-passed")
        insert(db, "dispatch_items", {"dispatch_id": did, "roll_id": r["id"]})
    return dispatch_detail(did, db)


@router.post("/dispatches/{did}/close")
def close_dispatch(did: int, db=Depends(get_db), _=role("admin", "store")):
    """Closes the truck and generates the invoice + packing list from the scanned rolls."""
    with tx(db):
        d = one(db, "SELECT d.*, o.price_per_m, f.gst_rate FROM dispatches d JOIN orders o ON o.id = d.order_id "
                    "JOIN fabric_types f ON f.id = o.fabric_type_id WHERE d.id = ?", did)
        if d["status"] != "open":
            raise HTTPException(409, "dispatch already closed")
        metres = db.execute("SELECT SUM(r.metres) FROM dispatch_items i JOIN rolls r ON r.id = i.roll_id "
                            "WHERE i.dispatch_id = ?", (did,)).fetchone()[0]
        if not metres:
            raise HTTPException(409, "no rolls scanned")
        db.execute("UPDATE rolls SET status = 'dispatched' WHERE id IN "
                   "(SELECT roll_id FROM dispatch_items WHERE dispatch_id = ?)", (did,))
        db.execute("UPDATE dispatches SET status = 'closed' WHERE id = ?", (did,))
        subtotal = round(metres * d["price_per_m"], 2)
        gst = round(subtotal * d["gst_rate"] / 100, 2)
        insert(db, "invoices", {"dispatch_id": did, "order_id": d["order_id"], "metres": metres,
                                "rate": d["price_per_m"], "subtotal": subtotal, "gst_rate": d["gst_rate"],
                                "gst": gst, "total": round(subtotal + gst, 2)})
    return dispatch_detail(did, db)


@router.get("/dispatches")
def dispatch_history(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("""SELECT d.*, b.name buyer, i.id invoice_id, i.total FROM dispatches d
        JOIN orders o ON o.id = d.order_id JOIN buyers b ON b.id = o.buyer_id
        LEFT JOIN invoices i ON i.dispatch_id = d.id ORDER BY d.id DESC"""))


@router.get("/dispatches/{did}")
def dispatch_detail(did: int, db=Depends(get_db), _=Depends(user)):
    """Dispatch with packing list and invoice (once closed)."""
    d = one(db, """SELECT d.*, b.name buyer, b.gstin buyer_gstin, b.address buyer_address, f.name fabric, o.colour
        FROM dispatches d JOIN orders o ON o.id = d.order_id JOIN buyers b ON b.id = o.buyer_id
        JOIN fabric_types f ON f.id = o.fabric_type_id WHERE d.id = ?""", did)
    packing = rows(db.execute("SELECT r.qr, r.metres FROM dispatch_items i JOIN rolls r ON r.id = i.roll_id "
                              "WHERE i.dispatch_id = ? ORDER BY i.id", (did,)))
    inv = db.execute("SELECT * FROM invoices WHERE dispatch_id = ?", (did,)).fetchone()
    return {**d, "packing_list": packing, "rolls": len(packing), "metres": sum(p["metres"] for p in packing),
            "invoice": dict(inv) if inv else None}
