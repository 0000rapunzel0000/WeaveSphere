"""The chat assistant (web page and WhatsApp): plain text in, checked requests out, run through the app's own API.

One turn:
 1. Jev picks the skill (mill/skills) the conversation is about, rates how hard the message is, and says whether it asks
    for one thing or several. Unsure of the skill -> the person picks from the top few.
    Several -> the chat model splits it into steps (a chain: a step can use what an earlier one creates) and every step
    goes through 1-4 on its own; Jev then checks the whole plan, and the steps run in order.
 2. The chat model fills that skill's fields from the conversation, using the mill's own names. Which model depends on
    Jev's difficulty rating: a light one for plain one-liners, a stronger one for informal or tangled messages.
 3. Code resolves every name to a record and checks every value; anything missing or unclear is asked back.
 4. Jev checks the finished request against the conversation. Confident -> it runs; not -> the person says YES / NO.
 5. It runs as one call to the normal API with the person's own sign-in, so every rule and permission applies.

A conversation is one request. It ends when the request is done, fails or is cancelled, on "thanks", after 30 quiet
minutes, or when the person clearly starts something else; the next message starts clean, with no old history."""
import difflib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
import os
import re
import secrets
import threading
from datetime import date, timedelta

import anyio.from_thread
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from mill import ai
from mill.auth import token_for, user
from mill.db import connect, get_db, insert, rows
from mill.production import roll_code
from mill.skills import SKILLS, Ref

router = APIRouter()
IDLE_MINUTES = 30
PLAN = "plan"            # the conversation's skill when one message asks for several things

# The chat model for a message, by how hard Jev rates it. Light and quick for a plain one-liner; strong for a tangle.
# Measured on this app's own messages (Oct 2026): flash-lite ~1.5 s but misses references like "#5" = Loom 02; 2.5-flash
# ~2-3 s; 3.7-flash ~6 s and resolves every reference; 3.1-pro and 3.8-flash were no better here and took 10-45 s.
MODELS = {"easy": os.environ.get("ASSISTANT_MODEL_EASY", "google/gemini-3.5-flash-lite"),
          "medium": os.environ.get("ASSISTANT_MODEL_MEDIUM", "google/gemini-2.5-flash"),
          "hard": os.environ.get("ASSISTANT_MODEL_HARD", "google/gemini-3.7-flash")}
DIFFICULTY = {
    "easy": "one clear request with every detail stated plainly, in simple words",
    "medium": "informal, abbreviated or mixed-language wording, or details that need working out (dates, short names)",
    "hard": "several requests at once, references to other records or people ('#5', 'he', 'it'), conditions "
            "('if he has other jobs'), corrections, or anything that needs careful reading"}
ACTIONS = {"one": "the message asks for one thing, done once (one entry to record, one change, or one question)",
           "several": "the message asks for two or more things to be done, including the same action more than once "
                      "('two rolls', 'both looms', 'Meena on loom 4 and Ravi on loom 1')"}


# Records a field can name: (id, label) per kind. Labels are what the chat model sees and what names are matched against.
CATALOG = {
    "machine": "SELECT id, name FROM machines ORDER BY name",
    "shift": "SELECT id, name FROM shifts ORDER BY id",
    "worker": "SELECT id, name FROM workers WHERE active = 1 ORDER BY name",
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
    return re.sub(r"[\W_]", "", re.sub(r"\d+", lambda m: str(int(m.group())), str(s).lower()))  # keeps Tamil/Hindi/Kannada letters


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


STEP_REF = re.compile(r"^@?step\s*(\d+)$", re.I)


def resolve(db, sk, given, refs=None):
    """given: {field: text the model wrote}; refs: {earlier step number: the kind it creates}. -> (values, names, problems).
    A value '@step2' means 'what step 2 creates': it is filled in when the plan runs."""
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
        if (ref := STEP_REF.match(raw)) and (refs or {}).get(int(ref.group(1))) == k:
            values[f.name] = Ref(int(ref.group(1)), k)
            names[f.name] = format(values[f.name])
            continue
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
    deferred = any(isinstance(x, Ref) for x in values.values())  # checked when the step runs, once the value exists
    if not problems and sk.prepare and not deferred:
        if p := sk.prepare(db, values, names):
            problems.append(p)
    if not problems:
        problems += [p for rule in sk.rules if (p := rule(values))]
    return values, names, problems


# ---------- Step 1: which skill (Jev) ----------
NONE = "none"
NONE_DESC = "greetings, thanks, small talk, or asking what the assistant can do; not a request for any of the other actions"


def route(lines, assess=False):
    """-> (skill or 'none', confidence, {skill: probability}, extra). Confidence 0 = Jev unavailable (the chat model picked).
    assess=True also asks Jev, in the same call, how hard the message is and whether it asks for several things."""
    opts = {n: s.desc for n, s in SKILLS.items()} | {NONE: NONE_DESC}
    qs = {"skill": (
        "A worker at a textile mill is chatting with the mill's assistant. Which one action do they want done? Go by their "
        "latest request. A question ('is…?', 'how much…?', 'who…?') is a LOOK UP; a statement of what happened is a RECORD.", opts)}
    if assess:
        qs |= {"difficulty": ("How hard is it to turn this message into exact requests for the mill's system?", DIFFICULTY),
               "actions": ("Does the message ask for one thing, or for several different things?", ACTIONS)}
    res = ai.decide({"conversation": lines}, qs, timeout=15)
    r = res["skill"]
    extra = {"difficulty": res["difficulty"]["choice"] if assess and res["difficulty"]["confidence"] > 0 else "medium",
             "several": bool(assess and res["actions"]["choice"] == "several" and res["actions"]["confidence"] >= 0.5)}
    if r["confidence"] > 0:
        return r["choice"], r["confidence"], r["probs"] or {}, extra
    try:  # Jev is down: let the chat model choose, and always have the person confirm the request later
        pick = ai.chat_json("Which action does this mill worker want? Answer with one action name.\nActions:\n" +
                            "\n".join(f"- {n}: {d}" for n, d in opts.items()) + "\n\nConversation:\n" + "\n".join(lines),
                            {"type": "object", "properties": {"skill": {"type": "string", "enum": list(opts)}}, "required": ["skill"]})
        return pick.get("skill", NONE), 0.0, {}, extra
    except Exception:
        return None, 0.0, {}, extra


# ---------- Step 2: the fields (chat model) ----------
FIELDS = {"type": "object", "properties": {
    "fields": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "value": {"type": "string"},
        "said": {"type": "string", "description": "the person's own words for this value, copied from the conversation"}},
        "required": ["name", "value", "said"]}},
    "question": {"type": "string"}}, "required": ["fields"]}


def floor_context(db):
    """What is on the floor now, so 'set #5 to running' or 'free Arun if he has other jobs' can be worked out."""
    out = []
    for r in db.execute("SELECT m.name, m.status, j.order_id, w.name, s.name FROM machines m LEFT JOIN job_cards j ON j.machine_id = m.id "
                        "AND j.status = 'open' LEFT JOIN workers w ON w.id = j.operator_id LEFT JOIN shifts s ON s.id = j.shift_id ORDER BY m.name"):
        out.append(f"{r[0]}: {r[1]}" + (f", running order #{r[2]}" if r[2] else ", no job")
                   + (f" with {r[3]}" if r[3] else (", no operator" if r[2] else "")) + (f", {r[4]} shift" if r[4] else ""))
    return "\n".join(out)


def extract(db, u, sk, lines, model=None, focus=None, refs=None):
    kinds = {f.kind for f in sk.fields} | ({WORKERS_KIND} if any(f.kind == "workers" for f in sk.fields) else set())
    known = "\n".join(f"{k}s: " + "; ".join(lbl for _, lbl in v) for k, v in catalog(db, kinds).items()) or "(none needed)"
    spec = "\n".join(f"- {f.name}{' (required)' if f.required else ''}: {f.label}"
                     + (f"; one of {', '.join(f.options)}" if f.options else "")
                     + {"date": "; YYYY-MM-DD", "month": "; YYYY-MM", "number": "; a plain number", "workers": "; names, comma-separated",
                        "roll": "; a roll code like ETM-1A2B3C4D5E"}.get(f.kind, "") for f in sk.fields)
    chain = ("\nEarlier steps in this same message create: " + "; ".join(f"step {n} → a new {k}" for n, k in refs.items())
             + ". If a field is what one of them creates, write the value as @stepN (e.g. @step1).\n") if refs else ""
    part = (f"\nThe person asked for several things; fill in ONLY this one, from the details written in it: \"{focus}\"\n"
            "This step was written from their message and the floor below: every detail it states (machine, order, shift, "
            "person) counts as what the person said - fill it in, and give its words as 'said'.\n" if focus else "")
    out = ai.chat_json(
        f"You fill in one request for a textile mill's system from a chat. Today is {date.today().isoformat()} ({date.today():%A}). "
        f"The person is {u['username']} ({u['role']}).\n\nRequest: {sk.name} — {sk.desc}\nFields:\n{spec}\n{part}{chain}\n"
        f"The mill's records (write names exactly as listed; orders, invoices, POs, trucks, reviews and messages by number):\n{known}\n\n"
        f"The floor right now:\n{floor_context(db)}\n\n"
        "Rules:\n" + ("- Fill the fields from the step's own words; use the conversation only to understand them. Other "
                      "steps and later messages about other machines or people do not change this step.\n" if focus else
                      "- Use only what the person said in this conversation. Never guess or fill in a value they did not give, "
                      "required or not (no default dates, no assumed people); leave it out.\n- Later messages correct earlier ones.\n")
        + "- For each field also give 'said': the exact words used for it.\n- Work out relative dates ('yesterday', 'Friday').\n"
        "- If a required field is missing or unclear, set 'question' to ONE short question asking for all of them, in the language "
        "the person writes in. Otherwise leave 'question' empty.\n\nConversation:\n" + "\n".join(lines), FIELDS, model)
    fields = [f for f in out.get("fields") or [] if isinstance(f, dict) and f.get("name")]
    return {f["name"]: f.get("value", "") for f in fields}, {f["name"]: f.get("said") or "" for f in fields}, (out.get("question") or "").strip()


FLOOR_KINDS = {"machine", "worker", "workers", "order", "shift", "job"}  # what the floor context names


def mentioned(src, words):
    """Were these words used? Whole, or by their distinctive parts: every number in them and at least one real word
    ('Meena Devi' when the person wrote 'Meena'; 'Loom 04' never for 'loom 2')."""
    if norm(src) and norm(src) in words:
        return True
    toks = [norm(t) for t in re.findall(r"\w+", src)]
    nums, names = [t for t in toks if t.isdigit()], [t for t in toks if len(t) >= 3 and not t.isdigit()]
    return bool(names) and all(t in words for t in nums) and any(t in words for t in names)


def grounded(sk, given, said, lines, floor=""):
    """Code's own check on the chat model: a name or number it filled in must come from words the person actually wrote.
    Anything else is dropped, so it gets asked for instead of being made up. Machine, worker, order and shift names may
    also come from the live floor (real data: '#5' is the order Loom 02 runs) - never from text a model wrote."""
    words = norm(" ".join(x for x in lines if x.startswith("user:")))
    known = norm(floor)
    kinds = {f.name: f.kind for f in sk.fields}
    keep = {}
    for k, v in given.items():
        kind, src = kinds.get(k), said.get(k, "")
        if STEP_REF.match(str(v).strip()):  # made by an earlier step: resolve() checks it is the right kind
            keep[k] = v
            continue
        if kind in ("choice", "text"):  # reworded ("broke down" -> Breakdown, a note): Jev judges these
            keep[k] = v
        elif kind in ("date", "month"):  # worked out from words the person used ("yesterday", "10 Oct"); never a default
            if src and mentioned(src, words):
                keep[k] = v
        elif kind == "number":
            n = number(v)
            if n is not None and norm(str(int(n)) if n == int(n) else str(n)) in words:
                keep[k] = v
        elif src and norm(src) and len(src.split()) <= 6 and (  # a name, not a whole sentence
                mentioned(src, words) or (kind in FLOOR_KINDS and mentioned(src, known))):
            keep[k] = v
    return keep


TURN = {"same": "it only gives what the assistant asked for, or changes or adds to the request under way; it asks for no other kind of work",
        "new": "it asks for a different kind of work than the request under way (logging output, a roll, attendance, a question…), "
               "even if it mentions the same machines or people"}


def new_request(msgs, text, kinds):
    """Jev: is the latest message part of the request under way, or a new one? A new one starts a clean conversation.
    Jev is shown the request, the kind of work it is (its skills), what we last asked, and the new message - not the
    transcript: with a transcript, or without the kind of work, it took 'Loom 4 did 120 m, order 1, Meena' for an answer
    to 'which order should Loom 04 run?'. Its pick counts even when unsure; if Jev is down, the request carries on."""
    said = [m_["text"] for m_ in msgs[:-1] if m_["from"] == "user"]
    asked = next((m_["text"] for m_ in reversed(msgs) if m_["from"] == "bot"), "")
    r = ai.decide({"request_under_way": " / ".join(said), "kind_of_work_under_way": [SKILLS[k].desc for k in dict.fromkeys(kinds) if k in SKILLS],
                   "assistant_last_said": asked, "latest_message": text}, {"turn": (
        "A mill worker is in the middle of a request with the mill's assistant. Is their latest message part of that request "
        "(only supplying what was asked, or correcting it), or a new, separate request - different work?", TURN)}, timeout=10)["turn"]
    if not r["confidence"] and not r["probs"]:
        return False  # Jev is down: carry on with the request
    new = r["probs"].get("new", 1.0 if r["choice"] == "new" else 0.0)
    # A message that reads as a whole request of its own (long, with a quantity or a second action) needs less to count as new:
    # "Loom 4 la 120 meter, order 1, Meena, 8 hours, loom 2 breakdown" while we wait for "which order should Loom 04 run?"
    whole = len(text.split()) >= 8 and bool(QUANTITY.search(text) or SECOND_ACTION.search(text))
    return new >= 0.5 or (whole and new >= 0.3)


# ---------- Several things in one message: a chain of steps ----------
SECOND_ACTION = re.compile(r"(\band\b|\balso\b|\bthen\b|[,;])\s+(also\s+|then\s+)?(mark|make|set|put|assign|issue|log|record|free|"
                           r"release|start|stop|open|load|close|pass|reject|add|approve|cancel|pay|book|give|take|move)\b", re.I)


def looks_several(text):
    """Code's backstop for Jev's one-or-several answer, which varied between runs on 'loom 4 did 120 m … and mark her
    present': a second action word after and / also / then. A false alarm only costs a split that returns one step."""
    return bool(SECOND_ACTION.search(text))


STEPS = {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}}}, "required": ["steps"]}


def split(db, u, lines, model):
    """The person's request as separate steps in the order to run them, each a complete instruction on its own."""
    out = ai.chat_json(
        f"A worker at a textile mill sent the mill's assistant a message that may ask for several things. Today is "
        f"{date.today().isoformat()}. Split what they want into separate steps, in the order they should be done.\n"
        "Rules:\n- Each step is ONE action, written as a complete instruction someone could act on alone.\n"
        "- Work out references from the floor below: '#5' is order #5 and the machine running it; 'he'/'him' is the person "
        "named; 'instead of X' names who is being replaced. A step about a machine starts with the machine's name "
        "(\"Set Loom 02 to Running\", not \"Set order #5's status\"). Starting or stopping a machine is \"Set <machine> to "
        "Running\" / \"Set <machine> to Idle\".\n- Keep every detail exactly as the person gave it. Add nothing, "
        "leave nothing out. A condition ('if he has other jobs') stays in its step.\n"
        "- Keep the person's own action: 'loom 4 did 120 m' or 'made 120 m' is output to log (\"Log 120 m on Loom 04 …\"); "
        "a roll only when they say roll.\n- Each step carries every detail it needs from anywhere in the message (machine, "
        "order, shift, person): 'and a roll from it too' becomes 'Make a 90 m roll for order #2 from Loom 01, day shift, Meena'.\n"
        "- An action done to several items (two rolls, three POs) is one step per item, except marking several workers "
        "present, which is one step. Never write the same step twice unless the person asked for it twice.\n- If a step uses something an earlier step creates (a new order, truck, roll, PO, buyer…), say 'the new <thing> "
        "from step N'.\n- Questions are steps too. If it is really one thing, return one step.\n\n"
        f"The floor right now:\n{floor_context(db)}\n\nConversation:\n" + "\n".join(lines), STEPS, model,
        os.environ.get("ASSISTANT_REASONING", "low"))  # measured: low thinking = ~2.5 s and steps as good as unlimited (~10-90 s)
    return [s_.strip() for s_ in out.get("steps") or [] if str(s_).strip()][:8]


class Later(dict):
    """Names for a step whose details are worked out when it runs (it uses an earlier step's result, so its prepare
    hasn't run yet): anything prepare would have named reads as a placeholder instead of failing."""
    def __missing__(self, key):
        return "(worked out when it runs)"


def build(db, u, sk, lines, model, focus=None, refs=None):
    """One request from the conversation -> (draft or None, problems, the model's question)."""
    given, said, question = ({}, {}, "") if not sk.fields else extract(db, u, sk, lines, model, focus, refs)
    given = grounded(sk, given, said, lines, floor_context(db) if focus else "")
    values, names, problems = resolve(db, sk, given, refs)
    if problems:
        return None, problems, question
    shown = Later(names) if any(isinstance(x, Ref) for x in values.values()) else names
    return {"skill": sk.name, "values": values, "names": names, "summary": sk.summary(values, shown), "said": said}, [], question


def dumps(d):
    return json.dumps(d, ensure_ascii=False, default=lambda o: {"$step": o.step, "kind": o.kind})


def loads(t):
    return json.loads(t, object_hook=lambda o: Ref(o["$step"], o["kind"]) if "$step" in o else o) if t else None


def cap(s_):
    return s_[:1].upper() + s_[1:].rstrip(".")  # the reply adds its own full stop


QUANTITY = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(m\b|mtrs?|metres?|meters?|kg|kgs|hrs?|hours?|rs\b|rupees|₹|%)", re.I)


def unit_of(u_):
    u_ = u_.lower()
    return "m" if u_.startswith("m") else "kg" if u_.startswith("kg") else "h" if u_.startswith("h") else u_ if u_ == "%" else "rs"


def unused_quantities(talk, plan):
    """Code's check on a plan: every quantity the person wrote (90 m, 8 hrs, 50 kg) must be in some step. One that isn't
    means a step was dropped or misread, so the plan is shown, never run straight away. A later message that gives the
    same kind of quantity ("actually 25 kg") replaces the earlier ones of that kind."""
    latest = {}  # unit -> quantities from the last message that gave that unit
    for line in (x for x in talk if x.startswith("user:")):
        found = {}
        for n, u_ in QUANTITY.findall(line):
            found.setdefault(unit_of(u_), {})[float(n.replace(",", ""))] = f"{n}{' ' if u_ not in ('%', '₹') else ''}{u_}"
        latest.update(found)
    used = {x for d in plan for x in d["values"].values() if isinstance(x, float)}
    return [txt for q in latest.values() for n, txt in q.items() if n not in used]


def unsupported(talk, plan):
    """Steps whose skill needs words the person never used (a roll when they only said 'did 120 m')."""
    said = " ".join(x for x in talk if x.startswith("user:"))
    return [f"a {d['skill'].split('_')[-1]} (step {i})" for i, d in enumerate(plan, 1)
            if SKILLS[d["skill"]].needs and not re.search(SKILLS[d["skill"]].needs, said, re.I)]


def other_jobs_only(plan):
    """'Free Arun from his other jobs' and 'put Arun on Loom 02' in one message: the job on Loom 02 is the one he is going
    to, so freeing him never touches it (whatever order the steps run in)."""
    going = {(d["values"].get("operator"), d["values"].get("machine")) for d in plan
             if d["skill"] in ("change_operator", "assign_job")}
    for d in plan:
        if d["skill"] == "release_worker" and not isinstance(d["values"].get("worker"), Ref):
            keep = {m_ for w_, m_ in going if w_ == d["values"]["worker"]}
            if keep and d["values"].get("_job_machines"):
                left = [(j, mc, txt) for j, mc, txt in zip(d["values"]["_jobs"], d["values"]["_job_machines"], d["names"]["_job_list"])
                        if mc not in keep]
                d["values"]["_jobs"] = [j for j, _, _ in left]
                d["names"]["_jobs"] = "; ".join(txt for _, _, txt in left)
                d["summary"] = SKILLS["release_worker"].summary(d["values"], d["names"])
    return plan


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
YES = re.compile(r"^\s*(yes|y|yeah|yep|ok|okay|confirm(ed)?|correct|sure|do it|go ahead|send it|haan|ha|sari|seri|howdu|houdu|ஆம்|हाँ|हां|ಹೌದು)\b", re.I)
NO = re.compile(r"^\s*(no|n|nope|wrong|nahi|illai|illa|இல்லை|नहीं|ಇಲ್ಲ)\b[\s.!]*$", re.I)
CANCEL = re.compile(r"^\s*(cancel|stop|forget it|never ?mind|reset|start over)\b", re.I)
THANKS = re.compile(r"^\s*(ok(ay)?\s*)?(thanks|thank you|thank u|thx|ty|tq|cheers|great|perfect|cool|nandri|நன்றி|dhanyavad|"
                    r"shukriya|dhanyavada|धन्यवाद|ಧನ್ಯವಾದ(ಗಳು)?)( a lot| so much)?[\s.!🙏👍]*$", re.I)
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
    draft = loads(conv["draft"])
    state = {"conv": conv, "msgs": msgs}

    def finish(reply, status, **cols):
        s_ = state["conv"]
        state["msgs"].append({"from": "bot", "text": reply})
        cols.update(status=status, messages=json.dumps(state["msgs"], ensure_ascii=False))
        db.execute(f"UPDATE bot_conversations SET updated_at = datetime('now'), {', '.join(k + ' = :' + k for k in cols)} WHERE id = :id",
                   {**cols, "id": s_["id"]})
        d = cols.get("draft", s_["draft"])
        return {"reply": reply, "status": status, "conversation_id": s_["id"], "skill": cols.get("skill", s_["skill"]),
                "jev": cols.get("jev"), "draft": json.loads(d) if d else None,
                "difficulty": cols.get("difficulty", s_.get("difficulty")), "model": cols.get("model", s_.get("model"))}

    def lines():
        return [f"{m['from']}: {m['text']}" for m in state["msgs"]]

    if CANCEL.match(text):
        return finish("Cancelled. Nothing was saved.", "cancelled")
    if conv["status"] == "confirming" and draft and "summary" in draft:
        if YES.match(text):
            return run_plan(db, draft["plan"], run, finish, conv["jev"]) if "plan" in draft else execute(db, draft, run, finish, conv["jev"])
        if NO.match(text):
            return finish("OK, nothing was saved. What should it be?", "collecting", draft=None)
    if THANKS.match(text):
        unsaved = conv["status"] == "confirming" and draft and draft.get("summary")
        return finish("You're welcome!" + (f" (Nothing was saved for: {unsaved}.)" if unsaved else ""), "closed")

    if conv["status"] == "collecting" and not conv["skill"] and len(msgs) == 1 and AMEND.match(text):
        last = loads((db.execute("SELECT draft FROM bot_conversations WHERE user_id = ? AND channel = ? AND peer = ? AND status = 'done' "
                                 "AND updated_at >= datetime('now', '-10 minutes') ORDER BY id DESC LIMIT 1", (u["id"], channel, peer)).fetchone()
                     or [None])[0])
        if last and ("plan" in last or not SKILLS[last["skill"]].read):
            return finish(f"That was already saved: {last['summary']}. Entries can't be undone from chat; "
                          "ask the office to correct it in the app, or tell me the new request in full.", "closed")

    skill, level, cols = conv["skill"], conv.get("difficulty"), {}
    choosing = bool(draft and draft.get("choices"))
    kinds = (draft or {}).get("skills") or [skill]  # a plan's skills, or this request's
    if skill and not choosing and len(text.split()) >= 3 and new_request(state["msgs"], text, kinds):
        finish("(Starting a new request.)", "closed")
        new_conversation(db, u, channel, peer)
        return turn(db, u, channel, peer, text, run)  # handled from scratch, with no trace of the old request
    if choosing:  # we asked "which one?"
        pick = text.strip().rstrip(".)")
        chosen = draft["choices"][int(pick) - 1] if pick.isdigit() and 0 < int(pick) <= len(draft["choices"]) else \
            next((c for c in draft["choices"] if norm(c) == norm(pick)), None)
        if chosen:
            skill = chosen
            state["msgs"] = msgs = msgs[:-1]  # the number isn't part of the request
    if not skill:
        name, conf, probs, x = route([m for m in lines() if m.startswith("user:")], assess=True)
        if name is None:
            return finish("Sorry, I can't read messages right now (the AI is unavailable). Please use the app.", "collecting")
        level = x["difficulty"]
        if x["several"] or looks_several(text):  # splitting a message is the hardest part: always the strongest model
            level = "hard"
            cols = {"difficulty": level, "model": MODELS[level]}
            return plan_turn(db, u, lines(), MODELS[level], run, finish, cols)
        cols = {"difficulty": level, "model": MODELS[level]}
        if name == NONE:
            return finish(HELP if re.search(r"\b(help|hi|hello|hey|what can you)\b|வணக்கம்|नमस्ते", text, re.I) else NOT_A_REQUEST,
                          "closed", **cols)
        if 0 < conf < ai.THRESHOLD:
            top = [n for n, p in sorted(probs.items(), key=lambda x_: -x_[1]) if n in SKILLS and p >= 0.1][:3] or [name]
            if len(top) > 1:
                short = lambda d: re.split(r"(?<=[a-z)])[:.] ", d, 1)[0]  # noqa: E731  "Change a machine's status", not the whole text
                return finish("Do you want to:\n" + "\n".join(f"{i}. {short(SKILLS[n].desc)}" for i, n in enumerate(top, 1))
                              + "\nReply with the number.", "collecting", draft=json.dumps({"choices": top}), **cols)
        skill = name
    elif skill == PLAN:
        # Answering our question: keep the steps. Changing a plan that waits for YES: read the whole conversation again.
        keep_steps = conv["status"] == "collecting" and draft and "steps" in draft
        return plan_turn(db, u, lines(), MODELS[level or "hard"], run, finish, {}, cached=draft if keep_steps else None)

    sk = SKILLS[skill]
    if not may(u, sk):
        return finish(f"Your role can't do that ({sk.desc[0].lower() + sk.desc[1:].rstrip('.')}). Ask the office.", "closed", skill=skill, **cols)
    d, problems, question = build(db, u, sk, lines(), MODELS[level or "medium"])
    if problems:
        missing_only = all(p_.startswith("the ") and "(" not in p_ for p_ in problems)  # nothing to explain beyond the question
        detail = "I still need " + "; ".join(problems) + "."
        reply = question if question and missing_only else f"{question}\n({detail})" if question else detail
        return finish(reply, "collecting", skill=skill, draft=None, **cols)
    if sk.read:
        return execute(db, d, run, finish, None, skill=skill, **cols)
    p = matches(lines(), worded(sk, d["values"], d["said"]))
    if p is not None and p >= ai.THRESHOLD:
        return execute(db, d, run, finish, p, skill=skill, **cols)
    why = "I couldn't double-check this automatically." if p is None else "Please check this before I save it."
    return finish(f"{why} I'll {d['summary']}. Reply YES to save it, or tell me what to change.", "confirming",
                  skill=skill, draft=dumps(d), jev=p, **cols)


def plan_turn(db, u, talk, model, run, finish, cols, cached=None):
    """Several things in one message: split into steps, build each one like a single request (a later step may use what
    an earlier one creates), then Jev checks the whole plan against the conversation.
    cached: the steps and their skills from an earlier turn of this plan. A follow-up answer only re-fills the fields,
    so the steps keep their numbers and nothing is split or routed again."""
    if cached:
        steps, routes = cached["steps"], [(n, c_, {}, {}) for n, c_ in zip(cached["skills"], cached["confs"])]
    else:
        steps = split(db, u, talk, model)
        if not steps:
            return finish(NOT_A_REQUEST, "closed", **cols)
        with ThreadPoolExecutor(4) as ex:
            routes = list(ex.map(lambda st: route([f"user: {st}"]), steps))
    if any(r[0] is None for r in routes):
        return finish("Sorry, I can't read messages right now (the AI is unavailable). Please use the app.", "collecting", skill=PLAN, **cols)
    unclear = [f"{i}. \"{st}\"" for i, (st, r) in enumerate(zip(steps, routes), 1) if r[0] not in SKILLS]
    if unclear:
        return finish("I didn't understand this part:\n" + "\n".join(unclear) + "\nSay it another way, or 'cancel'.", "collecting",
                      skill=PLAN, **cols)
    skills = [SKILLS[r[0]] for r in routes]
    if no := [f"{i}. {st}" for i, (st, sk) in enumerate(zip(steps, skills), 1) if not may(u, sk)]:
        return finish("Your role can't do this part, so nothing was saved:\n" + "\n".join(no) + "\nAsk the office.", "closed",
                      skill=PLAN, **cols)
    refs = lambda i: {j: skills[j - 1].gives[0] for j in range(1, i) if skills[j - 1].gives}  # noqa: E731
    def one_step(a):  # each thread gets its own connection: a SQLite connection must not be shared across threads at once
        conn = connect()
        try:
            return build(conn, u, a[1], talk, MODELS["medium"], focus=a[2], refs=refs(a[0]))  # steps are clear
        finally:
            conn.close()
    with ThreadPoolExecutor(4) as ex:
        built = list(ex.map(one_step, [(i, sk, st) for i, (sk, st) in enumerate(zip(skills, steps), 1)]))
    def need(pr):  # "I need the yarn; the kg" - prepare's own problems are whole sentences and stand alone
        parts = [p_ for p_ in pr if not p_[:1].isupper()]
        return " ".join([p_ for p_ in pr if p_[:1].isupper()] + (["I need " + "; ".join(parts)] if parts else []))
    asks = [f"{i}. {st}: " + (q if q and all(p_.startswith('the ') and '(' not in p_ for p_ in pr) else need(pr))
            for i, (st, (d, pr, q)) in enumerate(zip(steps, built), 1) if pr]
    memo = {"steps": steps, "skills": [r[0] for r in routes], "confs": [r[1] for r in routes]}
    if asks and cached and cached.get("asks") == asks:  # the answer didn't help: the steps or their skills were read wrong
        return plan_turn(db, u, talk, model, run, finish, cols)
    if asks:
        return finish("To do all of this I still need:\n" + "\n".join(asks), "collecting", skill=PLAN, draft=dumps({**memo, "asks": asks}), **cols)
    plan = other_jobs_only([d for d, _, _ in built])  # repeats are kept: "two 100 m rolls" is two steps on purpose
    summary = "\n".join(f"{i}. {cap(d['summary'])}" for i, d in enumerate(plan, 1))
    writes = [d for d in plan if not SKILLS[d["skill"]].read]
    p = matches(talk, "\n".join(f"{i}. {worded(SKILLS[d['skill']], d['values'], d['said'])}" for i, d in enumerate(plan, 1))) if writes else 1.0
    missing = unused_quantities(talk, plan) if writes else []
    unsaid = unsupported(talk, plan)
    sure = p is not None and p >= ai.THRESHOLD and all(r[1] >= ai.THRESHOLD for r in routes) and not missing and not unsaid
    full = {"plan": plan, "summary": summary, "skill": PLAN, **memo}
    if sure:
        return run_plan(db, plan, run, finish, p, skill=PLAN, draft=dumps(full), **cols)
    why = (f"You didn't mention {'; '.join(unsaid)}, so please check this before I save it." if unsaid
           else f"I haven't used {', '.join(missing)} from your message, so please check this before I save it." if missing
           else "I couldn't double-check this automatically." if p is None else "Please check this before I save it.")
    return finish(f"{why} I'll do this, in order:\n{summary}\nReply YES to save it all, or tell me what to change.", "confirming",
                  skill=PLAN, draft=dumps(full), jev=p, **cols)


def run_plan(db, plan, run, finish, jev, **cols):
    """The steps in order. A step that uses an earlier step's result gets it now. Stops at the first refusal."""
    made, out = {}, []
    keep = dict(cols, jev=jev)
    keep.setdefault("draft", dumps({"plan": plan, "summary": "\n".join(cap(d["summary"]) for d in plan), "skill": PLAN}))
    for i, d in enumerate(plan, 1):
        sk = SKILLS[d["skill"]]
        v, n = dict(d["values"]), dict(d["names"])
        later = [k for k, x in v.items() if isinstance(x, Ref)]
        for k in later:
            kind, v[k] = v[k].kind, made[v[k].step]
            n[k] = f"{kind} #{v[k]}" if isinstance(v[k], int) else str(v[k])
        problem = sk.prepare(db, v, n) if later and sk.prepare else None
        code, resp = (409, {"detail": problem}) if problem else run(*sk.request(v))
        summary = cap(sk.summary(v, n))
        if code >= 400:
            detail = resp.get("detail") if isinstance(resp, dict) else str(resp)
            rest = " Nothing after it was run." if i < len(plan) else ""
            return finish("\n".join(out + [f"{i}. Not saved: {summary}. {detail}.{rest}"]), "failed",
                          result=json.dumps(resp, default=str)[:4000], **keep)
        if sk.gives:
            made[i] = sk.gives[1](resp)
        extra = sk.done(resp, v, n) if sk.done else ""
        out.append(f"{i}. " + ((extra or "Done.") if sk.read else f"Done: {summary}." + (f" {extra}" if extra else "")))
    return finish("\n".join(out), "done", **keep)


def execute(db, d, run, finish, jev, **cols):
    sk = SKILLS[d["skill"]]
    method, path, body = sk.request(d["values"])
    code, resp = run(method, path, body)
    keep = dict(cols, draft=dumps(d), jev=jev)
    if code >= 400:
        detail = resp.get("detail") if isinstance(resp, dict) else str(resp)
        return finish(f"Not saved: {detail}" if not sk.read else f"Couldn't look that up: {detail}", "failed",
                      result=json.dumps(resp, default=str)[:4000], **keep)
    extra = sk.done(resp, d["values"], d["names"]) if sk.done else ""
    reply = (extra or "Done.") if sk.read else f"Done: {cap(d['summary'])}." + (f" {extra}" if extra else "")
    return finish(reply.strip(), "done", result=json.dumps(resp, default=str)[:4000], **keep)


_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()


def one_at_a_time(u, channel, peer):
    """A lock per sender: two quick messages from one person are handled one after the other, so neither overwrites the
    other's conversation. # ponytail: per process, like the sign-in lockout; a shared lock if the API ever runs as several."""
    with _locks_guard:
        return _locks.setdefault((u["id"], channel, peer), threading.Lock())


def reachable(handle):
    """The AI service can drop mid-message (no network, a timeout, a garbled answer). Say so plainly instead of a 500;
    nothing of that message was saved, so the person just sends it again."""
    try:
        return handle()
    except (OSError, ValueError) as e:  # URLError and timeouts are OSErrors; a garbled JSON answer is a ValueError
        logging.warning("assistant: AI unreachable: %r", e)
        return {"reply": "Sorry, I can't reach the AI right now, so nothing was saved. Please send that again in a minute, "
                         "or use the app.", "status": "unavailable"}


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
    with one_at_a_time(u, "web", ""):
        return reachable(lambda: turn(db, u, "web", "", body.text.strip(), api_runner(request.app, authorization)))


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


# ponytail: per-process memory of each sender's language; a table if the bot ever runs as several processes.
LAST_LANG: dict[str, str] = {}
SCRIPTS = (("Tamil in Tamil script", "[\u0B80-\u0BFF]"), ("Kannada in Kannada script", "[\u0C80-\u0CFF]"),
           ("Hindi in Devanagari script", "[\u0900-\u097F]"))
LANG_REPLY = {"type": "object", "properties": {"language": {"type": "string"}, "reply": {"type": "string"}},
              "required": ["language", "reply"]}


def in_their_language(phone, text, reply):
    """WhatsApp answers in the language and script the person wrote in (Tamil, Hindi, Kannada, Tanglish…).
    The reply is built in English; only its wording is translated. On any AI error the English reply goes out."""
    if not reply:
        return reply
    last = LAST_LANG.get(phone, "English")
    script = next((n for n, rx in SCRIPTS if re.search(rx, text)), None)  # native letters: no guessing needed
    if not script and last != "English" and len(re.findall(r"[^\W\d_]{3,}", text)) <= 1:  # "160 m", "yes", "ok"
        script = last
    keep = ("Translate only the connecting words. Copy these exactly, character for character, never translated or "
            "transliterated: every number, unit (m, kg, h), date, code (ETM-1001, #5, INV-00002, PO-2), person name "
            "(Arun Kumar), machine name (Loom 04), buyer, yarn and fabric name, status word in capitals, and YES. "
            f"Keep the line breaks and numbering.\n\nReply:\n{reply}")
    ask = (f"Translate this mill assistant's WhatsApp reply into {script}, for a mill worker. "
           f"Set language to '{script}'. {keep}") if script else (
        f"A mill worker sent this WhatsApp message:\n{text}\n\nWhich language is it in, and in which script? Answer e.g. "
        "'English', 'Tamil in Latin letters', 'Hindi in Latin letters', 'Kannada in Latin letters'. Tamil, Hindi or Kannada "
        "words typed in English letters (like 'evvalavu', 'aachu', 'kitna', 'hai', 'eshtu') make it that language in Latin "
        "letters, even if mixed with English words. Then give the reply below in that language and script; if it is "
        f"English, return the reply exactly as it is. {keep}")
    try:
        r = ai.chat_json(ask, LANG_REPLY, model=MODELS["medium"])
        lang, said = str(r["language"]).strip(), str(r["reply"]).strip()
    except Exception:  # AI down or a malformed answer: English is always safe
        return reply
    LAST_LANG[phone] = script or lang
    return reply if (lang.lower() == "english" and not script) or not said else said


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
    with one_at_a_time(u, "whatsapp", phone):
        res = reachable(lambda: turn(db, u, "whatsapp", phone, body.text.strip(), api_runner(request.app, "Bearer " + token_for(u, hours=0.1))))
    return {**res, "reply": in_their_language(phone, body.text.strip(), res.get("reply", ""))}


@router.get("/assistant/conversations")
def conversations(db=Depends(get_db), u=Depends(user)):
    """Recent conversations and what each became: the log of what the assistant did (owner and office see everyone's)."""
    every = u["role"] in ("owner", "admin")
    return [{**r, "messages": json.loads(r["messages"]), "draft": json.loads(r["draft"]) if r["draft"] else None}
            for r in rows(db.execute("SELECT c.id, c.channel, c.peer, c.status, c.skill, c.messages, c.draft, c.jev, c.difficulty, c.model, c.created_at, "
                                     "c.updated_at, us.username FROM bot_conversations c JOIN users us ON us.id = c.user_id "
                                     "WHERE ? OR c.user_id = ? ORDER BY c.id DESC LIMIT 50", (every, u["id"])))]
