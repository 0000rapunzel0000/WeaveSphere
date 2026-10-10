"""The chat assistant (web page and WhatsApp): plain text in, one checked request out, run through the app's own API.

One turn:
 1. Jev picks the skill (mill/skills) the conversation is about. Unsure -> the person picks from the top few.
 2. The chat model fills that skill's fields from the conversation, using the mill's own names.
 3. Code resolves every name to a record and checks every value; anything missing or unclear is asked back.
 4. Jev checks the finished request against the conversation. Confident -> it runs; not -> the person says YES / NO.
 5. It runs as one call to the normal API with the person's own sign-in, so every rule and permission applies.

A conversation is one request. It ends when the request is done, fails or is cancelled, on "thanks", after 30 quiet
minutes, or when the person clearly starts something else; the next message starts clean, with no old history."""
import difflib
import json
import os
import re
import secrets
from datetime import date, timedelta

import anyio.from_thread
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from mill import ai
from mill.auth import token_for, user
from mill.db import get_db, insert, rows
from mill.production import roll_code
from mill.skills import SKILLS

router = APIRouter()
IDLE_MINUTES = 30
SWITCH_CONFIDENCE = 0.6  # mid-request: a message Jev puts this sure on another skill (and <10% on this one) starts over

# Records a field can name: (id, label) per kind. Labels are what the chat model sees and what names are matched against.
CATALOG = {
    "machine": "SELECT id, name FROM machines ORDER BY name",
    "shift": "SELECT id, name FROM shifts ORDER BY id",
    "worker": "SELECT id, name FROM workers ORDER BY name",
    "yarn": "SELECT id, count || ' ' || material || ' ' || colour FROM yarn_types ORDER BY id",
    "supplier": "SELECT id, name FROM suppliers ORDER BY name",
    "buyer": "SELECT id, name FROM buyers ORDER BY name",
    "fabric": "SELECT id, name FROM fabric_types ORDER BY name",
    "order": "SELECT o.id, '#' || o.id || ' ' || b.name || ' · ' || f.name || ' · ' || o.colour || ' · ' || o.qty_m || ' m' "
             "FROM orders o JOIN buyers b ON b.id = o.buyer_id JOIN fabric_types f ON f.id = o.fabric_type_id ORDER BY o.id",
    "invoice": "SELECT i.id, 'INV-' || printf('%05d', i.id) || ' · ' || b.name || ' · order #' || i.order_id || ' · due ' || "
               "printf('%.2f', i.total - COALESCE((SELECT SUM(amount) FROM payments WHERE invoice_id = i.id), 0)) "
               "FROM invoices i JOIN orders o ON o.id = i.order_id JOIN buyers b ON b.id = o.buyer_id ORDER BY i.id",
    "po": "SELECT p.id, 'PO-' || p.id || ' · ' || s.name || ' · ' || p.status FROM purchase_orders p "
          "JOIN suppliers s ON s.id = p.supplier_id WHERE p.status != 'cancelled' ORDER BY p.id",
    "truck": "SELECT d.id, 'truck #' || d.id || ' · ' || d.vehicle_no || ' · order #' || d.order_id || ' · ' || d.status "
             "FROM dispatches d ORDER BY d.status = 'open' DESC, d.id DESC LIMIT 20",
    "review": "SELECT id, 'review #' || id || ' · ' || kind || ' · ' || status || ' · ' || COALESCE(final, suggestion) FROM ai_reviews "
              "ORDER BY status = 'open' DESC, id DESC LIMIT 20",
    "alert": "SELECT id, 'alert #' || id || ' · ' || label FROM defect_alerts WHERE status IN ('open', 'acked') ORDER BY id",
    "message": "SELECT m.id, 'message #' || m.id || ' · ' || COALESCE(b.name, 'unknown buyer') || ': ' || substr(m.text, 1, 60) "
               "FROM buyer_messages m LEFT JOIN buyers b ON b.id = m.buyer_id ORDER BY m.id DESC LIMIT 15",
    "job": "SELECT j.id, m.name || ' · order #' || j.order_id FROM job_cards j JOIN machines m ON m.id = j.machine_id "
           "WHERE j.status = 'open' ORDER BY m.name",
}
BY_NUMBER = {"order", "invoice", "po", "truck", "review", "message", "alert"}  # "2", "#2", "PO-2", "INV-00002" all work
WORKERS_KIND = "worker"


def catalog(db, kinds):
    return {k: [(r[0], r[1]) for r in db.execute(CATALOG[k])] for k in kinds if k in CATALOG}


# ---------- Names -> records ----------
def norm(s):
    """'Loom 04' and 'loom4' are the same machine."""
    return re.sub(r"[\W_]", "", re.sub(r"\d+", lambda m: str(int(m.group())), str(s).lower()))  # keeps Tamil/Hindi letters


def match(value, cands):
    """cands: [(id, label)]. Exact (normalised), else the one label containing every word given, else one close spelling."""
    v = norm(value)
    if not v:
        return None
    exact = [i for i, lbl in cands if norm(lbl) == v]
    if len(exact) == 1:
        return exact[0]
    words = [norm(w) for w in re.findall(r"[A-Za-z]+|\d+", str(value))]
    part = [i for i, lbl in cands if all(w in norm(lbl) for w in words)]
    if len(part) == 1:
        return part[0]
    if part:  # "loom" with three looms: ask which
        return None
    # A near spelling ("Meenaa", "polyster") only counts when the numbers are identical: Loom 01 is never Loom 04.
    digits = lambda s: re.findall(r"\d+", norm(s))  # noqa: E731
    same = [(i, lbl) for i, lbl in cands if digits(lbl) == digits(value)]
    close = difflib.get_close_matches(v, [norm(lbl) for _, lbl in same], n=2, cutoff=0.8)
    return next(i for i, lbl in same if norm(lbl) == close[0]) if len(close) == 1 else None


def number(v):
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v))
    return float(m.group().replace(",", "")) if m else None


def when(raw):
    low, t = raw.strip().lower(), date.today()
    d = {"today": t, "yesterday": t - timedelta(days=1), "tomorrow": t + timedelta(days=1)}.get(low)
    try:
        return (d or date.fromisoformat(raw.strip()[:10])).isoformat()
    except ValueError:
        return None


def month(raw):
    low, t = raw.strip().lower(), date.today()
    if low in ("this month", "current month"):
        return t.strftime("%Y-%m")
    if low in ("last month", "previous month"):
        return (t.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return raw.strip()[:7] if re.fullmatch(r"\d{4}-\d{2}", raw.strip()[:7]) else None


def resolve(db, sk, given):
    """given: {field: text the model wrote}. -> (values, names, problems)."""
    cat = catalog(db, {f.kind for f in sk.fields} | ({WORKERS_KIND} if any(f.kind == "workers" for f in sk.fields) else set()))
    values, names, problems = {}, {}, []
    listing = lambda k: ", ".join(lbl for _, lbl in cat[k][:12]) or "none yet"  # noqa: E731
    for f in sk.fields:
        raw = str(given.get(f.name, "")).strip()
        if not raw or raw.lower() in ("none", "null", "n/a", "-"):
            if f.required:
                problems.append(f"the {f.label}")
            continue
        k, bad = f.kind, None
        if k == "number":
            x = number(raw)
            values[f.name], bad = x, (x is None or x < 0)
        elif k == "date":
            values[f.name] = when(raw)
            bad = values[f.name] is None
        elif k == "month":
            values[f.name] = month(raw)
            bad = values[f.name] is None
        elif k == "choice":
            values[f.name] = next((o for o in f.options if o.lower() == raw.lower().replace(" ", "_")
                                   or o.lower() == raw.lower()), None)
            bad = values[f.name] is None and f"the {f.label} (one of: {', '.join(f.options)})"
        elif k == "text":
            values[f.name] = raw
        elif k == "roll":
            code = roll_code(raw).upper()
            values[f.name] = names[f.name] = code
            bad = not db.execute("SELECT 1 FROM rolls WHERE qr = ?", (code,)).fetchone() and f"the roll code (there is no roll {code})"
        elif k == "workers":
            ids, unknown = [], []
            for part in re.split(r",|\band\b|&|\+", raw):
                if part.strip():
                    w = match(part, cat[WORKERS_KIND])
                    ids.append(w) if w else unknown.append(part.strip())
            values[f.name], names[f.name] = ids, ", ".join(dict(cat[WORKERS_KIND])[i] for i in ids)
            bad = (unknown or not ids) and f"who {', '.join(unknown) or raw} is (workers: {listing(WORKERS_KIND)})"
        else:  # a record
            x = None
            if k in BY_NUMBER and (n := number(re.sub(r"(?i)^(po|inv|truck|review|message|alert|order)[-\s#]*", "", raw))) is not None:
                x = int(n) if int(n) in dict(cat[k]) else None
            x = x or match(raw, cat[k])
            values[f.name] = x
            if x:
                names[f.name] = dict(cat[k])[x]
            bad = not x and f"which {f.label} '{raw}' is ({listing(k)})"
        if bad:
            problems.append(bad if isinstance(bad, str) else f"the {f.label} ('{raw}' isn't a valid {f.label})")
    if not problems and sk.prepare:
        if p := sk.prepare(db, values, names):
            problems.append(p)
    if not problems:
        problems += [p for rule in sk.rules if (p := rule(values))]
    return values, names, problems


# ---------- Step 1: which skill (Jev) ----------
NONE = "none"
NONE_DESC = "greetings, thanks, small talk, or asking what the assistant can do; not a request for any of the other actions"


def route(lines):
    """-> (skill or 'none', confidence, {skill: probability}). Confidence 0 = Jev unavailable (the chat model picked)."""
    opts = {n: s.desc for n, s in SKILLS.items()} | {NONE: NONE_DESC}
    r = ai.decide({"conversation": lines}, {"skill": (
        "A worker at a textile mill is chatting with the mill's assistant. Which one action do they want done? Go by their "
        "latest request. A question ('is…?', 'how much…?', 'who…?') is a LOOK UP; a statement of what happened is a RECORD.",
        opts)}, timeout=15)["skill"]
    if r["confidence"] > 0:
        return r["choice"], r["confidence"], r["probs"] or {}
    try:  # Jev is down: let the chat model choose, and always have the person confirm the request later
        pick = ai.chat_json("Which action does this mill worker want? Answer with one action name.\nActions:\n" +
                            "\n".join(f"- {n}: {d}" for n, d in opts.items()) + "\n\nConversation:\n" + "\n".join(lines),
                            {"type": "object", "properties": {"skill": {"type": "string", "enum": list(opts)}}, "required": ["skill"]})
        return pick.get("skill", NONE), 0.0, {}
    except Exception:
        return None, 0.0, {}


# ---------- Step 2: the fields (chat model) ----------
FIELDS = {"type": "object", "properties": {
    "fields": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "value": {"type": "string"},
        "said": {"type": "string", "description": "the person's own words for this value, copied from the conversation"}},
        "required": ["name", "value", "said"]}},
    "question": {"type": "string"}}, "required": ["fields"]}


def extract(db, u, sk, lines):
    kinds = {f.kind for f in sk.fields} | ({WORKERS_KIND} if any(f.kind == "workers" for f in sk.fields) else set())
    known = "\n".join(f"{k}s: " + "; ".join(lbl for _, lbl in v) for k, v in catalog(db, kinds).items()) or "(none needed)"
    spec = "\n".join(f"- {f.name}{' (required)' if f.required else ''}: {f.label}"
                     + (f"; one of {', '.join(f.options)}" if f.options else "")
                     + {"date": "; YYYY-MM-DD", "month": "; YYYY-MM", "number": "; a plain number", "workers": "; names, comma-separated",
                        "roll": "; a roll code like ETM-1A2B3C4D5E"}.get(f.kind, "") for f in sk.fields)
    out = ai.chat_json(
        f"You fill in one request for a textile mill's system from a chat. Today is {date.today().isoformat()} ({date.today():%A}). "
        f"The person is {u['username']} ({u['role']}).\n\nRequest: {sk.name} — {sk.desc}\nFields:\n{spec}\n\n"
        f"The mill's records (write names exactly as listed; orders, invoices, POs, trucks, reviews and messages by number):\n{known}\n\n"
        "Rules:\n- Use only what the person said in this conversation. Never guess or fill in a value they did not give, required "
        "or not (no default dates, no assumed people); leave it out.\n- For each field also give 'said': the exact words the person used "
        "for it, copied from their messages.\n"
        "- Later messages correct earlier ones.\n- Work out relative dates ('yesterday', 'Friday').\n"
        "- If a required field is missing or unclear, set 'question' to ONE short question asking for all of them, in the language "
        "the person writes in. Otherwise leave 'question' empty.\n\nConversation:\n" + "\n".join(lines), FIELDS)
    fields = [f for f in out.get("fields") or [] if isinstance(f, dict) and f.get("name")]
    return {f["name"]: f.get("value", "") for f in fields}, {f["name"]: f.get("said") or "" for f in fields}, (out.get("question") or "").strip()


def grounded(sk, given, said, lines):
    """Code's own check on the chat model: a name or number it filled in must come from words the person actually wrote.
    Anything else is dropped, so it gets asked for instead of being made up."""
    words = norm(" ".join(x for x in lines if x.startswith("user:")))
    kinds = {f.name: f.kind for f in sk.fields}
    keep = {}
    for k, v in given.items():
        kind, src = kinds.get(k), said.get(k, "")
        if kind in ("date", "month", "choice", "text"):  # worked out or reworded ("yesterday", "broke down"): Jev judges these
            keep[k] = v
        elif kind == "number":
            n = number(v)
            if n is not None and norm(str(int(n)) if n == int(n) else str(n)) in words:
                keep[k] = v
        elif src and norm(src) and norm(src) in words:
            keep[k] = v
    return keep


# ---------- Step 4: does the request match the conversation (Jev) ----------
def worded(sk, values, said):
    """The request in the person's own words for names (code matched those to records itself) and the worked-out values
    for numbers, dates and choices. Jev compares this with the conversation; full record names would read as changes."""
    parts = []
    for f in sk.fields:
        if f.name in values and values[f.name] not in (None, "", []):
            v = values[f.name] if f.kind in ("number", "date", "month", "choice") else (said.get(f.name) or values[f.name])
            parts.append(f"{f.label.split(' (')[0]} {v:g}" if isinstance(v, float) else f"{f.label.split(' (')[0]} {v}")
    return sk.name.replace("_", " ") + ": " + ", ".join(parts)


def matches(lines, request):
    return ai.probability(
        {"today": date.today().isoformat(), "conversation": lines, "request_to_run": request},
        "A mill worker chatted with the mill's assistant, which is about to run the request below. Does the request do what the "
        "person asked for, with no number, date, item or person added, changed or left out?",
        "the request matches what the person asked for", "the request adds, changes or misses something the person said")


# ---------- One turn ----------
YES = re.compile(r"^\s*(yes|y|yeah|yep|ok|okay|confirm(ed)?|correct|sure|do it|go ahead|send it|haan|ha|sari|seri|ஆம்|हाँ|हां)\b", re.I)
NO = re.compile(r"^\s*(no|n|nope|wrong|nahi|illai|இல்லை|नहीं)\b[\s.!]*$", re.I)
CANCEL = re.compile(r"^\s*(cancel|stop|forget it|never ?mind|reset|start over)\b", re.I)
THANKS = re.compile(r"^\s*(ok(ay)?\s*)?(thanks|thank you|thank u|thx|ty|tq|cheers|great|perfect|cool|nandri|நன்றி|dhanyavad|"
                    r"shukriya|धन्यवाद)( a lot| so much)?[\s.!🙏👍]*$", re.I)
AMEND = re.compile(r"^\s*(no wait|wait|sorry|oops|actually|make it|change (it|that)|that was wrong|wrong|undo)\b", re.I)
NOT_A_REQUEST = ("I didn't catch a request there. I start fresh after each one, so tell me the whole thing in one message, e.g. "
                 "\"Loom 01 did 100 m on night shift for order 1, Arun, 8 hours\". Say \"help\" to see what I can do.")
HELP = ("I can do anything the app does: log shift output, make rolls, change machine status, issue or receive yarn, approve "
        "POs, record QC results, load and close trucks, book orders, log buyer messages, record payments, mark attendance, "
        "add setup data, and look up orders, stock, the floor, wages or the P&L. Tell me in your own words.")


def open_conversation(db, u, channel, peer):
    r = db.execute("SELECT * FROM bot_conversations WHERE user_id = ? AND channel = ? AND peer = ? "
                   "AND status IN ('collecting', 'confirming') AND updated_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
                   (u["id"], channel, peer, f"-{IDLE_MINUTES} minutes")).fetchone()
    if r:
        return dict(r)
    return new_conversation(db, u, channel, peer)


def new_conversation(db, u, channel, peer):
    db.execute("UPDATE bot_conversations SET status = 'closed' WHERE user_id = ? AND channel = ? AND peer = ? "
               "AND status IN ('collecting', 'confirming')", (u["id"], channel, peer))
    cid = insert(db, "bot_conversations", {"user_id": u["id"], "channel": channel, "peer": peer})
    return dict(db.execute("SELECT * FROM bot_conversations WHERE id = ?", (cid,)).fetchone())


def may(u, sk):
    return u["role"] == "owner" or u["role"] in sk.roles


def turn(db, u, channel, peer, text, run):
    """One message from the person -> the reply. run(method, path, body) calls the API as this person."""
    conv = open_conversation(db, u, channel, peer)
    msgs = json.loads(conv["messages"]) + [{"from": "user", "text": text}]
    draft = json.loads(conv["draft"]) if conv["draft"] else None
    state = {"conv": conv, "msgs": msgs}

    def finish(reply, status, **cols):
        s = state["conv"]
        state["msgs"].append({"from": "bot", "text": reply})
        cols.update(status=status, messages=json.dumps(state["msgs"], ensure_ascii=False))
        db.execute(f"UPDATE bot_conversations SET updated_at = datetime('now'), {', '.join(k + ' = :' + k for k in cols)} WHERE id = :id",
                   {**cols, "id": s["id"]})
        d = cols.get("draft", s["draft"])
        return {"reply": reply, "status": status, "conversation_id": s["id"], "skill": cols.get("skill", s["skill"]),
                "jev": cols.get("jev"), "draft": json.loads(d) if d else None}

    def lines():
        return [f"{m['from']}: {m['text']}" for m in state["msgs"]]

    if CANCEL.match(text):
        return finish("Cancelled. Nothing was saved.", "cancelled")
    if conv["status"] == "confirming" and draft and "summary" in draft:
        if YES.match(text):
            return execute(draft, run, finish, conv["jev"])
        if NO.match(text):
            return finish("OK, nothing was saved. What should it be?", "collecting", draft=None)
    if THANKS.match(text):
        unsaved = conv["status"] == "confirming" and draft and draft.get("summary")
        return finish("You're welcome!" + (f" (Nothing was saved for: {unsaved}.)" if unsaved else ""), "closed")

    if conv["status"] == "collecting" and not conv["skill"] and len(msgs) == 1 and AMEND.match(text):
        last = db.execute("SELECT draft FROM bot_conversations WHERE user_id = ? AND channel = ? AND peer = ? AND status = 'done' "
                          "AND updated_at >= datetime('now', '-10 minutes') ORDER BY id DESC LIMIT 1", (u["id"], channel, peer)).fetchone()
        if last and last["draft"] and not SKILLS[json.loads(last["draft"])["skill"]].read:
            return finish(f"That was already saved: {json.loads(last['draft'])['summary']}. Entries can't be undone from chat; "
                          "ask the office to correct it in the app, or tell me the new request in full.", "closed")

    skill = conv["skill"]
    if draft and draft.get("choices"):  # we asked "which one?"
        pick = text.strip().rstrip(".)")
        chosen = draft["choices"][int(pick) - 1] if pick.isdigit() and 0 < int(pick) <= len(draft["choices"]) else \
            next((c for c in draft["choices"] if norm(c) == norm(pick)), None)
        if chosen:
            skill = chosen
            state["msgs"] = msgs = msgs[:-1]  # the number isn't part of the request
    if not skill:
        name, conf, probs = route([m for m in lines() if m.startswith("user:")])
        if name is None:
            return finish("Sorry, I can't read messages right now (the AI is unavailable). Please use the app.", "collecting")
        if name == NONE:
            return finish(HELP if re.search(r"\b(help|hi|hello|hey|what can you)\b|வணக்கம்|नमस्ते", text, re.I) else NOT_A_REQUEST, "closed")
        if 0 < conf < ai.THRESHOLD:
            top = [n for n, p in sorted(probs.items(), key=lambda x: -x[1]) if n in SKILLS and p >= 0.1][:3] or [name]
            if len(top) > 1:
                short = lambda d: re.split(r"(?<=[a-z)])[:.] ", d, 1)[0]  # noqa: E731  "Change a machine's status", not the whole text
                return finish("Do you want to:\n" + "\n".join(f"{i}. {short(SKILLS[n].desc)}" for i, n in enumerate(top, 1))
                              + "\nReply with the number.", "collecting", draft=json.dumps({"choices": top}))
        skill = name
    elif not (draft and draft.get("choices")) and len(text.split()) >= 3:
        # Mid-request: is this a new, different request? Then it starts a clean conversation of its own.
        name, conf, probs = route([f"user: {text}"])
        if name in SKILLS and name != skill and conf >= SWITCH_CONFIDENCE and probs.get(skill, 0) < 0.1:
            finish("(Starting a new request.)", "closed")
            state["conv"] = new_conversation(db, u, channel, peer)
            state["msgs"] = msgs = [{"from": "user", "text": text}]
            skill = name

    sk = SKILLS[skill]
    if not may(u, sk):
        return finish(f"Your role can't do that ({sk.desc[0].lower() + sk.desc[1:].rstrip('.')}). Ask the office.", "closed", skill=skill)
    given, said, question = ({}, {}, "") if not sk.fields else extract(db, u, sk, lines())
    given = grounded(sk, given, said, lines())
    values, names, problems = resolve(db, sk, given)
    if problems:
        missing_only = all(p_.startswith("the ") and "(" not in p_ for p_ in problems)  # nothing to explain beyond the question
        detail = "I still need " + "; ".join(problems) + "."
        reply = question if question and missing_only else f"{question}\n({detail})" if question else detail
        return finish(reply, "collecting", skill=skill, draft=None)
    summary = sk.summary(values, names)
    d = {"skill": skill, "values": values, "names": names, "summary": summary, "said": said}
    if sk.read:
        return execute(d, run, finish, None, skill=skill)
    p = matches(lines(), worded(sk, values, said))
    if p is not None and p >= ai.THRESHOLD:
        return execute(d, run, finish, p, skill=skill)
    why = "I couldn't double-check this automatically." if p is None else "Please check this before I save it."
    return finish(f"{why} I'll {summary}. Reply YES to save it, or tell me what to change.", "confirming",
                  skill=skill, draft=json.dumps(d, ensure_ascii=False), jev=p)


def execute(d, run, finish, jev, **cols):
    sk = SKILLS[d["skill"]]
    method, path, body = sk.request(d["values"])
    code, resp = run(method, path, body)
    keep = dict(cols, draft=json.dumps(d, ensure_ascii=False), jev=jev)
    if code >= 400:
        detail = resp.get("detail") if isinstance(resp, dict) else str(resp)
        return finish(f"Not saved: {detail}" if not sk.read else f"Couldn't look that up: {detail}", "failed",
                      result=json.dumps(resp, default=str)[:4000], **keep)
    extra = sk.done(resp, d["values"], d["names"]) if sk.done else ""
    reply = (extra or "Done.") if sk.read else f"Done: {d['summary'][0].upper() + d['summary'][1:]}." + (f" {extra}" if extra else "")
    return finish(reply.strip(), "done", result=json.dumps(resp, default=str)[:4000], **keep)


def api_runner(app, authorization):
    """Calls the app's own API in-process, as the person who is chatting. Runs from a request's worker thread."""
    def run(method, path, body):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mill", timeout=90) as cl:
                return await cl.request(method, path, json=body, headers={"Authorization": authorization})
        r = anyio.from_thread.run(go)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text}
    return run


# ---------- Endpoints ----------
class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/assistant/chat")
def chat(body: ChatIn, request: Request, authorization: str = Header(""), db=Depends(get_db), u=Depends(user)):
    return turn(db, u, "web", "", body.text.strip(), api_runner(request.app, authorization))


@router.get("/assistant/chat")
def chat_history(db=Depends(get_db), u=Depends(user)):
    """Your recent web conversations, oldest first, so the page can show the thread."""
    convs = rows(db.execute("SELECT id, status, skill, messages FROM bot_conversations WHERE user_id = ? AND channel = 'web' "
                            "ORDER BY id DESC LIMIT 6", (u["id"],)))[::-1]
    return [{**c, "messages": json.loads(c["messages"])} for c in convs]


class WhatsAppIn(BaseModel):
    phone: str = Field(min_length=5, max_length=32)
    text: str = Field(min_length=1, max_length=2000)


def phone_links():
    """BOT_PHONES=919876543210=floor,919812345678=store : which WhatsApp number is which mill account (demo-sized)."""
    out = {}
    for pair in os.environ.get("BOT_PHONES", "").split(","):
        num, _, name = pair.partition("=")
        if re.sub(r"\D", "", num) and name.strip():
            out[re.sub(r"\D", "", num)] = name.strip()
    return out


@router.post("/assistant/whatsapp")
def whatsapp(body: WhatsAppIn, request: Request, x_bot_secret: str = Header(""), db=Depends(get_db)):
    """Called by whatsapp/bot.mjs for each incoming message; the reply goes back to the sender."""
    secret = os.environ.get("BOT_SECRET", "")
    if not secret or not secrets.compare_digest(x_bot_secret.encode(), secret.encode()):
        raise HTTPException(401, "bad bot secret")
    phone = re.sub(r"\D", "", body.phone)
    name = phone_links().get(phone)
    u = name and db.execute("SELECT id, username, role FROM users WHERE username = ?", (name,)).fetchone()
    if not u:
        return {"reply": "This number isn't linked to a mill account. Ask the office to add it.", "status": "unlinked"}
    u = dict(u)
    return turn(db, u, "whatsapp", phone, body.text.strip(), api_runner(request.app, "Bearer " + token_for(u, hours=0.1)))


@router.get("/assistant/conversations")
def conversations(db=Depends(get_db), u=Depends(user)):
    """Recent conversations and what each became: the log of what the assistant did (owner and office see everyone's)."""
    every = u["role"] in ("owner", "admin")
    return [{**r, "messages": json.loads(r["messages"]), "draft": json.loads(r["draft"]) if r["draft"] else None}
            for r in rows(db.execute("SELECT c.id, c.channel, c.peer, c.status, c.skill, c.messages, c.draft, c.jev, c.created_at, "
                                     "c.updated_at, us.username FROM bot_conversations c JOIN users us ON us.id = c.user_id "
                                     "WHERE ? OR c.user_id = ? ORDER BY c.id DESC LIMIT 50", (every, u["id"])))]
