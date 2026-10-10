"""Phone agent for buyers (Retell). Retell runs the conversation; these endpoints are the facts it may look up,
and every finished call lands in the Inbox as a phone message that Jev routes like any other.
Only requests signed with RETELL_API_KEY (or carrying VOICE_SECRET) are served, and a caller only ever hears about
their own orders."""
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from functools import cache

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from mill.db import connect, get_db, rows
from mill.messages import message_context
from mill.orders import tracking
from mill.reviews import decide

router = APIRouter(prefix="/voice/retell")  # what Retell calls; the only part exposed through the tunnel
web = APIRouter(prefix="/voice")  # the browser phone; main app only, never tunnelled (each call costs money)


def verify(raw: bytes, header: str | None):
    """X-Retell-Signature is "v=<ms timestamp>,d=<hex HMAC-SHA256 of body + timestamp, keyed with the API key>"."""
    key = os.environ.get("RETELL_API_KEY", "")
    m = re.fullmatch(r"v=(\d+),d=([0-9a-f]+)", header or "")
    if not key or not m:
        raise HTTPException(401, "unsigned request")
    ts, digest = m.groups()
    if abs(time.time() * 1000 - int(ts)) > 5 * 60 * 1000:
        raise HTTPException(401, "stale signature")
    if not hmac.compare_digest(hmac.new(key.encode(), raw + ts.encode(), hashlib.sha256).hexdigest(), digest):
        raise HTTPException(401, "bad signature")


async def signed(request: Request):
    """Retell signs with the account's webhook-badge API key only. With any other key, the agent's tools send
    VOICE_SECRET as a header and the webhook URL carries it as ?s=, so either proof is accepted."""
    raw = await request.body()
    secret = os.environ.get("VOICE_SECRET", "")
    given = request.headers.get("x-voice-secret") or request.query_params.get("s") or ""
    if not (len(secret) >= 32 and hmac.compare_digest(given, secret)):
        verify(raw, request.headers.get("x-retell-signature"))
    return json.loads(raw or b"{}")


# ---------- who is calling ----------

def last10(phone):
    return re.sub(r"\D", "", phone or "")[-10:]


def norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def caller(db, call):
    """Who is calling: for a browser call opened from a tracking link, that order's buyer (our server put the token in
    the call's metadata, the browser can't); for a phone call, the buyer whose number matches the caller ID."""
    token = ((call or {}).get("metadata") or {}).get("track_token")
    if token:
        return db.execute("SELECT b.id, b.name, b.phone, o.id order_id FROM orders o JOIN buyers b ON b.id = o.buyer_id "
                          "WHERE o.token = ?", (token,)).fetchone()
    num = last10((call or {}).get("from_number"))
    if len(num) < 10:
        return None
    return next((b for b in rows(db.execute("SELECT id, name, phone FROM buyers WHERE phone IS NOT NULL"))
                 if last10(b["phone"]) == num), None)


def verified_order(db, call, args):
    """The order the caller asked about, only if it is theirs: matched by caller ID, or, when the number is unknown,
    by the company name they gave. Caller ID can be spoofed, so an order number is always required as well."""
    # ponytail: name-or-caller-ID is demo-grade identity; add a one-time code over WhatsApp before real money talk.
    n = re.sub(r"\D", "", str(args.get("order_number", "")))
    o = n and db.execute("SELECT o.*, b.name buyer FROM orders o JOIN buyers b ON b.id = o.buyer_id WHERE o.id = ?",
                         (int(n),)).fetchone()
    if not o:
        return None
    who = caller(db, call)
    if who:
        return dict(o) if who["id"] == o["buyer_id"] else None
    said, real = norm(args.get("company_name")), norm(o["buyer"])
    return dict(o) if len(said) >= 4 and (said in real or real in said) else None


NOT_VERIFIED = {"verified": False, "say": "I couldn't match that order to your company. Please check the order number "
                                         "and company name, or I can ask the office to call you back."}


# ---------- tools ----------

def identify_caller(db, call, args):
    b = caller(db, call)
    if not b:
        return {"known": False}
    open_orders = db.execute("SELECT COUNT(*) FROM orders o WHERE buyer_id = ? AND (SELECT COALESCE(SUM(metres), 0) "
                             "FROM rolls WHERE order_id = o.id AND status = 'dispatched') < o.qty_m", (b["id"],)).fetchone()[0]
    out = {"known": True, "company": b["name"], "open_orders": open_orders}
    if "order_id" in b.keys():
        out["calling_from_tracking_link_of_order"] = b["order_id"]
    return out


def order_status(db, call, args):
    o = verified_order(db, call, args)
    if not o:
        return NOT_VERIFIED
    t = tracking(db, o)
    return {"verified": True, "order_number": o["id"], "company": o["buyer"], "fabric": t["fabric"], "colour": t["colour"],
            "stage": t["stage"], "ordered_metres": t["target_m"], "made_metres": t["produced_m"],
            "passed_quality_check_metres": t["passed_m"], "dispatched_metres": t["dispatched_m"],
            "progress_percent": t["progress_pct"], "expected_ready_date": t["expected_ready"] or "not estimated yet",
            "deadline": t["deadline"],
            "trucks": [{"vehicle": d["vehicle_no"], "status": "left the mill" if d["status"] == "closed" else "loading",
                        "rolls": d["rolls"], "metres": d["metres"]} for d in t["dispatches"]]}


def invoices_due(db, call, args):
    o = verified_order(db, call, args)
    if not o:
        return NOT_VERIFIED
    due = rows(db.execute("""SELECT i.id, i.issued_on, i.total,
          i.total - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = i.id), 0) outstanding
        FROM invoices i JOIN orders o ON o.id = i.order_id WHERE o.buyer_id = ? ORDER BY i.id""", (o["buyer_id"],)))
    return {"verified": True, "company": o["buyer"], "currency": "INR",
            "invoices": [{"invoice": f"INV-{d['id']:05d}", "issued_on": d["issued_on"], "total": round(d["total"], 2),
                          "outstanding": round(d["outstanding"], 2)} for d in due if d["outstanding"] > 0.005]}


TOOLS = {"identify_caller": identify_caller, "order_status": order_status, "invoices_due": invoices_due}


def run_tool(name, body):
    db = connect()
    try:
        return TOOLS[name](db, body.get("call"), body.get("args") or {})
    finally:
        db.close()


@router.post("/tools/{name}")
async def tool(name: str, request: Request):
    body = await signed(request)
    if name not in TOOLS:
        raise HTTPException(404, "unknown tool")
    return await run_in_threadpool(run_tool, name, body)


# ---------- after the call: into the Inbox ----------

def log_call(call):
    """One Inbox message per call (call_id is unique), routed by Jev from the summary."""
    summary = ((call.get("call_analysis") or {}).get("call_summary") or "").strip()
    transcript = (call.get("transcript") or "").strip()
    if not (summary or transcript):
        return None
    db = connect()
    try:
        b = caller(db, call)
        text = f"Phone call: {summary or transcript[:400]}" + (f"\n\nTranscript:\n{transcript}" if transcript else "")
        cur = db.execute("INSERT OR IGNORE INTO buyer_messages (buyer_id, channel, text, external_id) VALUES (?, 'phone', ?, ?)",
                         (b and b["id"], text, call.get("call_id")))
        if not cur.rowcount:
            return None  # Retell retried a call we already logged
        msg = dict(db.execute("SELECT * FROM buyer_messages WHERE id = ?", (cur.lastrowid,)).fetchone())
        routing = decide(db, "message", msg["id"], {"text": summary or transcript, "orders": message_context(db, msg)})
        db.execute("UPDATE buyer_messages SET confidence = ? WHERE id = ?", (routing["confidence"], msg["id"]))
        return msg["id"]
    finally:
        db.close()


@router.post("/webhook")
async def webhook(request: Request):
    body = await signed(request)
    if body.get("event") == "call_analyzed":
        await run_in_threadpool(log_call, body.get("call") or {})
    return {"ok": True}


# ---------- the browser phone ----------

def retell(method, path, body=None):
    req = urllib.request.Request("https://api.retellai.com" + path, json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Authorization": f"Bearer {os.environ.get('RETELL_API_KEY', '')}",
                                                         "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"voice service: {e.code} {e.read().decode()[:200]}")


@cache
def agent_id():
    return os.environ.get("RETELL_AGENT_ID") or next(
        (a["agent_id"] for a in retell("GET", "/list-agents") if a.get("agent_name") == "Everest buyer line"), None)


class WebCallIn(BaseModel):
    track: str | None = None  # the buyer's tracking token, when the call page was opened from their link


# ponytail: per-process, per-IP limit; put it in a table or the proxy if this ever runs as several processes.
RECENT_CALLS: dict[str, list[float]] = {}


@web.post("/web-call")
def web_call(body: WebCallIn, request: Request, db=Depends(get_db)):
    """Start a browser call to the order desk. The Retell key stays here; the browser only gets a one-call token."""
    if not os.environ.get("RETELL_API_KEY") or not agent_id():
        raise HTTPException(503, "The order desk line is not set up yet.")
    ip, now = request.client.host if request.client else "?", time.time()
    recent = [t for t in RECENT_CALLS.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many calls from here; please try again in a few minutes.")
    RECENT_CALLS[ip] = recent + [now]
    meta, about = {}, None
    if body.track:
        o = db.execute("SELECT o.id, b.name FROM orders o JOIN buyers b ON b.id = o.buyer_id WHERE o.token = ?",
                       (body.track,)).fetchone()
        if not o:
            raise HTTPException(404, "That tracking link is not valid.")
        meta, about = {"track_token": body.track}, {"order": o["id"], "company": o["name"]}
    r = retell("POST", "/v3/create-web-call", {"agent_id": agent_id(), "metadata": meta})
    return {"access_token": r["access_token"], "call_id": r["call_id"], "transport": r.get("transport"),
            "url": r.get("url"), "ice_servers": r.get("ice_servers"), "about": about}
