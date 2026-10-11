"""Master data: buyers, suppliers, yarn and fabric types, BOM, machines, workers, shifts."""
from fastapi import APIRouter, Depends, HTTPException

from mill.auth import role, user
from mill.db import get_db, insert, one, rows

router = APIRouter()


MASTERS = {"buyers", "suppliers", "yarn_types", "fabric_types", "bom", "machines", "workers", "shifts"}


def master_cols(db, table, data):
    if table not in MASTERS:
        raise HTTPException(404, "unknown master table")
    cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")} - {"id"}
    cols.discard("active")  # removing a worker goes through DELETE /workers/{id}
    if table == "machines":
        cols.discard("status")  # changed only via /machines/{id}/status so it gets logged
    if bad := set(data) - cols:
        raise HTTPException(422, f"unknown fields: {sorted(bad)}")


@router.get("/master/{table}")
def master_list(table: str, db=Depends(get_db), _=Depends(user)):
    master_cols(db, table, {})
    return rows(db.execute(f"SELECT * FROM {table} ORDER BY id"))


@router.post("/master/{table}")
def master_create(table: str, data: dict, db=Depends(get_db), _=role("admin")):
    master_cols(db, table, data)
    if not data:
        raise HTTPException(422, "send the fields for the new record")
    return one(db, f"SELECT * FROM {table} WHERE id = ?", insert(db, table, data))


@router.put("/master/{table}/{id}")
def master_update(table: str, id: int, data: dict, db=Depends(get_db), _=role("admin")):
    master_cols(db, table, data)
    if data:
        db.execute(f"UPDATE {table} SET {', '.join(k + ' = :' + k for k in data)} WHERE id = :_id", {**data, "_id": id})
    return one(db, f"SELECT * FROM {table} WHERE id = ?", id)
