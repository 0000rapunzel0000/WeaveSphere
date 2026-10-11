"""Buyer messages: Jev routing and AI reply drafts from live order data."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mill import ai
from mill.auth import role
from mill.db import get_db, insert, one, rows
from mill.orders import tracking
from mill.reviews import decide

router = APIRouter()


class MessageIn(BaseModel):
    text: str = Field(min_length=1)
    channel: Literal["whatsapp", "email", "phone"] = "whatsapp"
    buyer_id: int | None = None
    order_id: int | None = None


class DraftIn(BaseModel):
    lang: Literal["en", "ta", "hi", "kn"] = "en"


class ReplyIn(BaseModel):
    text: str = Field(min_length=1)


def message_context(db, msg):
    """All of this buyer's orders (the one the message was logged against first), never another buyer's.
    Buyers often ask about a different order than the one the office picked."""
    if not msg["buyer_id"]:
        return []
    return [tracking(db, o) for o in rows(db.execute(
        "SELECT * FROM orders WHERE buyer_id = ? ORDER BY id = ? DESC, id", (msg["buyer_id"], msg["order_id"])))]


def catalogue(db):
    """What the mill weaves, so general enquiries ("what fabrics do you sell?") get a real answer."""
    return rows(db.execute(
        "SELECT f.name AS fabric, group_concat(y.count || ' ' || y.material, ', ') AS yarns FROM fabric_types f "
        "LEFT JOIN bom b ON b.fabric_type_id = f.id LEFT JOIN yarn_types y ON y.id = b.yarn_type_id "
        "GROUP BY f.id ORDER BY f.name"))


@router.post("/messages")
def receive_message(body: MessageIn, db=Depends(get_db), _=role("admin")):
    data = body.model_dump()
    if body.order_id:
        owner = one(db, "SELECT buyer_id FROM orders WHERE id = ?", body.order_id)["buyer_id"]
        if body.buyer_id and body.buyer_id != owner:
            raise HTTPException(422, f"Order #{body.order_id} belongs to a different buyer.")
        data["buyer_id"] = owner
    mid = insert(db, "buyer_messages", data)
    msg = one(db, "SELECT * FROM buyer_messages WHERE id = ?", mid)
    routing = decide(db, "message", mid, {"text": body.text, "orders": message_context(db, msg)})
    db.execute("UPDATE buyer_messages SET confidence = ? WHERE id = ?", (routing["confidence"], mid))
    return {"message": one(db, "SELECT * FROM buyer_messages WHERE id = ?", mid), "routing": routing}


@router.get("/messages")
def list_messages(status: str | None = None, dept: str | None = None, db=Depends(get_db), _=role("admin")):
    return rows(db.execute("SELECT * FROM buyer_messages WHERE (? IS NULL OR status = ?) AND (? IS NULL OR dept = ?) "
                           "ORDER BY id DESC", (status, status, dept, dept)))


@router.post("/messages/{mid}/draft")
def draft_reply(mid: int, body: DraftIn, db=Depends(get_db), _=role("admin")):
    msg = one(db, "SELECT * FROM buyer_messages WHERE id = ?", mid)
    try:
        text = ai.draft_reply(msg["text"], {"orders": message_context(db, msg), "catalogue": catalogue(db)}, body.lang)
    except Exception as e:
        raise HTTPException(503, f"reply drafter unavailable: {e}")
    db.execute("UPDATE buyer_messages SET reply = ? WHERE id = ?", (text, mid))
    return {"draft": text}


@router.post("/messages/{mid}/reply")
def mark_replied(mid: int, body: ReplyIn, db=Depends(get_db), _=role("admin")):
    """Records the reply that was sent. Delivery over WhatsApp/email is the integration's job."""
    one(db, "SELECT id FROM buyer_messages WHERE id = ?", mid)
    db.execute("UPDATE buyer_messages SET reply = ?, status = 'replied' WHERE id = ?", (body.text, mid))
    return one(db, "SELECT * FROM buyer_messages WHERE id = ?", mid)
