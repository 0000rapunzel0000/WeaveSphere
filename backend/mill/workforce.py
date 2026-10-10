"""Attendance and wages (daily rate or piece rate)."""
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from mill.auth import role
from mill.db import get_db, month_of, rows, tx

router = APIRouter()


class AttendanceIn(BaseModel):
    shift_id: int
    work_date: date = Field(default_factory=date.today)
    worker_ids: list[int]


@router.post("/attendance")
def mark_attendance(body: AttendanceIn, db=Depends(get_db), _=role("admin", "supervisor")):
    with tx(db):
        for w in body.worker_ids:
            db.execute("INSERT OR IGNORE INTO attendance (worker_id, shift_id, work_date) VALUES (?, ?, ?)",
                       (w, body.shift_id, body.work_date.isoformat()))
    return rows(db.execute("SELECT * FROM attendance WHERE shift_id = ? AND work_date = ?",
                           (body.shift_id, body.work_date.isoformat())))


@router.get("/attendance")
def attendance_register(month: str | None = None, db=Depends(get_db), _=role("admin", "supervisor")):
    """Who was present, day by day, for one month (YYYY-MM, default this month): one record per worker per shift."""
    m = month_of(month)
    return {"month": m, "records": rows(db.execute(
        "SELECT a.worker_id, a.work_date, a.shift_id, s.name shift FROM attendance a JOIN shifts s ON s.id = a.shift_id "
        "WHERE strftime('%Y-%m', a.work_date) = ? ORDER BY a.work_date, s.name", (m,)))}


def wages(db, month):
    # ponytail: daily rate per day present (double shifts not paid extra; overtime is out of scope).
    out = rows(db.execute("""SELECT w.id, w.name, w.wage_type, w.rate,
          (SELECT COUNT(DISTINCT work_date) FROM attendance WHERE worker_id = w.id AND strftime('%Y-%m', work_date) = :m) days,
          (SELECT COALESCE(SUM(output), 0) FROM production_entries WHERE operator_id = w.id AND strftime('%Y-%m', entry_date) = :m) output
        FROM workers w ORDER BY w.id""", {"m": month}))
    for w in out:
        w["wage"] = round(w["rate"] * (w["days"] if w["wage_type"] == "daily" else w["output"]), 2)
    return out


@router.get("/wages")
def wages_report(month: str | None = None, db=Depends(get_db), _=role("admin")):
    w = wages(db, month_of(month))
    return {"month": month_of(month), "workers": w, "total": round(sum(x["wage"] for x in w), 2)}
