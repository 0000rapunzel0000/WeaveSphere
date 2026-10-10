"""Quality control: the QC queue and inspection results (defects classified by Jev)."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mill.auth import role, user
from mill.db import get_db, insert, one, rows, tx
from mill.defects import after_qc
from mill.reviews import decide

router = APIRouter()


class QcIn(BaseModel):
    result: Literal["pass", "rework", "reject"]
    defect_type: Literal["hole", "colour_mismatch", "uneven_weave", "stain", "other"] | None = None
    notes: str | None = None


@router.get("/qc/queue")
def qc_queue(db=Depends(get_db), _=Depends(user)):
    return rows(db.execute("""SELECT r.*, m.name machine FROM rolls r JOIN machines m ON m.id = r.machine_id
        WHERE r.status IN ('pending_qc', 'rework') ORDER BY r.id"""))


@router.post("/qc/{qr}")
def qc_check(qr: str, body: QcIn, db=Depends(get_db), u=role("admin", "qc")):
    if body.result != "pass" and not (body.defect_type or body.notes):
        raise HTTPException(422, "give a defect type or notes for rework/reject")
    with tx(db):
        r = one(db, "SELECT * FROM rolls WHERE qr = ?", qr)
        if r["status"] not in ("pending_qc", "rework"):
            raise HTTPException(409, f"roll is {r['status']}, not awaiting QC")
        rec = body.model_dump()
        if body.result == "pass":
            rec["defect_type"] = None
        qid = insert(db, "qc_records", {"roll_id": r["id"], **rec, "inspector_id": u["id"]})
        db.execute("UPDATE rolls SET status = ? WHERE id = ?",
                   ({"pass": "passed", "rework": "rework", "reject": "rejected"}[body.result], r["id"]))
    ai_result = None
    if body.result != "pass" and body.notes:
        m = one(db, "SELECT name, kind FROM machines WHERE id = ?", r["machine_id"])
        ai_result = decide(db, "defect", qid, {"note": body.notes, "machine": m["name"], "machine_kind": m["kind"]},
                           skip={"defect_type"} if body.defect_type else ())
    return {"qc": one(db, "SELECT * FROM qc_records WHERE id = ?", qid), "classification": ai_result,
            "defect_alerts": after_qc(db, qid)}  # alerts this check newly opened
