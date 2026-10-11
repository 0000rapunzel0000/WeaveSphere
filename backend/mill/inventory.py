"""Yarn store: goods receipts (GRN), challan photo reading, live stock, and issues to production."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mill import ai
from mill.auth import role, user
from mill.db import get_db, insert, one, rows, tx
from mill.purchasing import auto_po, check_po_line

router = APIRouter()


class GrnIn(BaseModel):
    supplier_id: int
    yarn_type_id: int
    qty_kg: float = Field(gt=0)
    rate: float = Field(ge=0)
    order_id: int | None = None
    challan_no: str | None = None
    po_line_id: int | None = None  # book this delivery against an approved purchase-order line


class WriteOffIn(BaseModel):
    yarn_type_id: int
    qty_kg: float = Field(gt=0)
    reason: Literal["damaged", "wastage", "count_correction", "returned_to_supplier", "other"]
    note: str | None = Field(None, max_length=500)


class IssueIn(BaseModel):
    order_id: int
    yarn_type_id: int
    qty_kg: float = Field(gt=0)
    machine_id: int | None = None


class ChallanIn(BaseModel):
    image_b64: str = Field(min_length=1, max_length=14_000_000)  # ~10 MB photo
    mime: Literal["image/jpeg", "image/png", "image/webp", "image/heic"] = "image/jpeg"


LOW_STOCK = "SELECT * FROM yarn_stock WHERE reorder_kg > 0 AND stock_kg <= reorder_kg"


@router.post("/grn")
def create_grn(body: GrnIn, db=Depends(get_db), u=role("admin", "store")):
    with tx(db):
        if body.po_line_id:
            check_po_line(db, body.po_line_id, body.supplier_id, body.yarn_type_id)
        gid = insert(db, "grn", {**body.model_dump(exclude={"po_line_id"}), "created_by": u["id"]})
        if body.po_line_id:
            insert(db, "po_receipts", {"po_line_id": body.po_line_id, "grn_id": gid})
    return {"grn": one(db, "SELECT * FROM grn WHERE id = ?", gid),
            "stock": one(db, "SELECT * FROM yarn_stock WHERE id = ?", body.yarn_type_id),
            "low_stock": rows(db.execute(LOW_STOCK)), "po_refresh": auto_po(db)}


@router.get("/grn")
def list_grn(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT g.*, s.name supplier FROM grn g JOIN suppliers s ON s.id = g.supplier_id "
                           "ORDER BY g.id DESC"))


@router.post("/grn/challan")
def grn_from_challan(body: ChallanIn, db=Depends(get_db), _=role("admin", "store")):
    """Photo of challan -> suggested GRN fields. Store keeper confirms by POSTing /grn."""
    try:
        f = ai.read_challan(body.image_b64, body.mime)
    except Exception as e:
        raise HTTPException(503, f"challan reader unavailable: {e}")
    norm = lambda v: str(v or "").strip().lower()  # noqa: E731  exact match, ignoring case and spaces
    supplier = db.execute("SELECT id FROM suppliers WHERE lower(trim(name)) = ?", (norm(f.get("supplier")),)).fetchone()
    yarn = db.execute("SELECT id FROM yarn_types WHERE lower(trim(count)) = ? AND lower(trim(material)) = ? "
                      "AND lower(trim(colour)) = ?",
                      (norm(f.get("yarn_count")), norm(f.get("material")), norm(f.get("colour")))).fetchone()
    return {"extracted": f, "suggested": {"supplier_id": supplier and supplier[0], "yarn_type_id": yarn and yarn[0],
                                          "qty_kg": f.get("qty_kg"), "rate": f.get("rate"),
                                          "challan_no": f.get("challan_no")}}


@router.get("/stock")
def stock(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT *, reorder_kg > 0 AND stock_kg <= reorder_kg low FROM yarn_stock"))


@router.get("/alerts/low-stock")
def low_stock(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute(LOW_STOCK))


@router.post("/issues")
def issue_yarn(body: IssueIn, db=Depends(get_db), u=role("admin", "store")):
    with tx(db):
        s = one(db, "SELECT stock_kg, avg_rate FROM yarn_stock WHERE id = ?", body.yarn_type_id)
        if body.qty_kg - s["stock_kg"] > 1e-6:  # float sums: issuing exactly what's on hand must work
            raise HTTPException(409, f"only {s['stock_kg']:g} kg in stock")
        iid = insert(db, "material_issues", {**body.model_dump(), "rate": s["avg_rate"], "created_by": u["id"]})
    return {"issue": one(db, "SELECT * FROM material_issues WHERE id = ?", iid),
            "low_stock": rows(db.execute(LOW_STOCK)), "po_refresh": auto_po(db)}


@router.post("/stock/writeoffs")
def write_off(body: WriteOffIn, db=Depends(get_db), u=role("admin", "store")):
    """Takes yarn out of stock without an order (damaged, wasted, short on a count). Never below zero."""
    with tx(db):
        s = one(db, "SELECT stock_kg, avg_rate FROM yarn_stock WHERE id = ?", body.yarn_type_id)
        if body.qty_kg - s["stock_kg"] > 1e-6:
            raise HTTPException(409, f"only {s['stock_kg']:g} kg in stock")
        wid = insert(db, "stock_writeoffs", {**body.model_dump(), "rate": s["avg_rate"], "created_by": u["id"]})
    return {"writeoff": one(db, "SELECT * FROM stock_writeoffs WHERE id = ?", wid),
            "low_stock": rows(db.execute(LOW_STOCK)), "po_refresh": auto_po(db)}


@router.get("/stock/writeoffs")
def list_writeoffs(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT w.*, us.username by_user FROM stock_writeoffs w LEFT JOIN users us ON us.id = w.created_by "
                           "ORDER BY w.id DESC"))


@router.get("/issues")
def list_issues(order_id: int | None = None, db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("SELECT * FROM material_issues WHERE ? IS NULL OR order_id = ? ORDER BY id DESC",
                           (order_id, order_id)))
