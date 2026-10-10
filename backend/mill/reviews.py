"""AI decisions (Jev) with a confidence gate, and the human review queue that corrects them."""
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from mill import ai
from mill.auth import role
from mill.db import get_db, insert, one, rows, tx

router = APIRouter()


# Option descriptions are what Jev reads, so they decide how sure it can be. Edit them, not the code.
HEADS = {  # kind -> (table, {field: (Jev instructions, {option: description})}); first field is the one the gate checks
    "message": ("buyer_messages", {
        "dept": ("Which mill department should handle this buyer message?", {
            "Office": "general enquiries, documents, order confirmations, anything the office can answer",
            "QC": "complaints about fabric quality, defects, shade, weight or width",
            "Production": "when goods will be ready, how far an order has progressed in the mill",
            "Dispatch": "trucks, delivery dates and addresses, missing or delayed shipments",
            "Accounts": "invoices, payments, dues, GST, credit notes"}),
        "intent": ("What does the buyer want?", {
            "status": "asking where an order is or when it will be ready",
            "quality_complaint": "complaining about defects, colour, weight or quality of fabric received",
            "payment": "about invoices, payments, dues or GST",
            "new_order": "wants to place a new order or get a quote",
            "delivery_change": "wants to change the delivery date, address or quantity",
            "other": "greetings, acknowledgements, or anything unclear that fits none of the above"}),
        "urgency": ("How urgently does the mill need to respond?", {
            "low": "nothing is at stake, it can wait a day",
            "med": "should be answered today",
            "high": "the buyer's production, a deadline or money is at risk; answer within the hour"})}),
    "breakdown": ("machine_status_log", {
        "category": ("What kind of fault caused this machine breakdown?", {
            "mechanical": "belts, bearings, gears, shuttle or needle damage, jams, worn or broken parts",
            "electrical": "motor, wiring, power trips, sensors, drives, control panel faults",
            "yarn_quality": "yarn snapping, slubs, weak or uneven yarn, knots, wrong count",
            "operator": "set-up or handling mistake, wrong settings, threading error"}),
        "severity": ("How badly does this breakdown hurt production?", {
            "low": "the machine can keep running or be fixed in minutes",
            "med": "the machine is stopped for a few hours",
            "high": "the machine is stopped for a shift or more, or an order deadline is at risk"})}),
    "defect": ("qc_records", {
        "defect_type": ("What kind of fabric defect does the inspector describe?", {
            "hole": "holes, tears, missing threads, broken picks",
            "colour_mismatch": "shade differs from the approved colour, patchy or uneven dyeing",
            "uneven_weave": "irregular density or tension, bars, slubs, pattern faults",
            "stain": "oil, dirt, dye or water marks",
            "other": "anything that fits none of the above"}),
        "stage_at_fault": ("Which production stage most likely caused the defect?", {
            "spinning": "caused while making the yarn (count, twist, slubs)",
            "weaving": "caused on the loom (holes, tension, broken ends)",
            "dyeing": "caused in dyeing or finishing (colour, stains, shade)"}),
        "severity": ("How serious is this defect for the buyer?", {
            "low": "cosmetic, within normal tolerance",
            "med": "clearly visible, the buyer may demand a discount or rework",
            "high": "unsellable, the buyer would reject the roll"})}),
}
# Who may see and settle each kind of decision (the owner always may).
REVIEWERS = {"message": {"admin"}, "breakdown": {"admin", "supervisor"}, "defect": {"admin", "qc"}}
# Who fixes a breakdown follows from its category: a rule, not an AI call.
ASSIGNEE_FOR = {"mechanical": "fitter", "electrical": "electrician", "yarn_quality": "store", "operator": "supervisor"}


def apply_decision(db, kind, ref_id, fields):
    table = HEADS[kind][0]
    if kind == "breakdown" and "category" in fields:
        fields = {**fields, "assignee": ASSIGNEE_FOR[fields["category"]]}
    sets = [f"{k} = ?" for k in fields] + (["status = CASE status WHEN 'replied' THEN status ELSE 'routed' END"]
                                           if kind == "message" else [])
    db.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", (*fields.values(), ref_id))


def decide(db, kind, ref_id, state, skip=()):
    """Confidence gate on the primary decision (dept / category / defect); the other fields ride along as hints.
    A message Jev can only call "other" can't be routed, so a human sees it whatever the confidence."""
    res = ai.decide(state, {f: q for f, q in HEADS[kind][1].items() if f not in skip})
    choice = {f: r["choice"] for f, r in res.items()}
    conf = next(iter(res.values()))["confidence"]
    if kind == "breakdown":
        choice["assignee"] = ASSIGNEE_FOR[choice["category"]]
    auto = conf >= ai.THRESHOLD and choice.get("intent") != "other"
    if auto:  # a field Jev gave no valid answer for (confidence 0) is left for a person, not filled with a default
        apply_decision(db, kind, ref_id, {f: v for f, v in choice.items() if f in res and res[f]["confidence"] > 0})
    rid = insert(db, "ai_reviews", {"kind": kind, "ref_id": ref_id, "suggestion": json.dumps(choice), "confidence": conf,
                                    "auto": int(auto), "status": "resolved" if auto else "open"})
    return {"auto": auto, "confidence": round(conf, 3), "suggestion": choice, "review_id": rid,
            "probs": {f: r["probs"] for f, r in res.items()}}


def review_row(r):
    return {**r, "auto": bool(r["auto"]), "suggestion": json.loads(r["suggestion"]),
            "final": json.loads(r["final"]) if r["final"] else None}


@router.get("/reviews")
def review_queue(status: Literal["open", "resolved", "all"] = "open", kind: str | None = None,
                 db=Depends(get_db), u=role("admin", "supervisor", "qc")):
    """open = the human queue; resolved = auto-applied or already decided (still correctable). Only your team's kinds."""
    return [review_row(r) for r in rows(db.execute(
        "SELECT * FROM ai_reviews WHERE (? = 'all' OR status = ?) AND (? IS NULL OR kind = ?) ORDER BY id DESC",
        (status, status, kind, kind))) if may_review(u, r["kind"])]


def may_review(u, kind):
    return u["role"] == "owner" or u["role"] in REVIEWERS[kind]


@router.get("/reviews/accuracy")
def review_accuracy(db=Depends(get_db), _=role("admin")):
    """How often Jev's main decision matched what a person settled on, by confidence band.
    This is the evidence for AI_THRESHOLD: pick the lowest band whose accuracy you can live with."""
    bands = [(0.9, 1.01, "0.9-1.0"), (0.8, 0.9, "0.8-0.9"), (0.7, 0.8, "0.7-0.8"), (0.0, 0.7, "below 0.7")]
    out = {}
    for r in rows(db.execute("SELECT * FROM ai_reviews WHERE confidence > 0")):  # 0 = AI was down, not a decision
        k = out.setdefault(r["kind"], {"decisions": 0, "auto_applied": 0, "human_checked": 0,
                                       "bands": {b: {"checked": 0, "agreed": 0} for _, _, b in bands}})
        k["decisions"] += 1
        k["auto_applied"] += r["auto"]
        if r["final"]:
            suggestion, final = json.loads(r["suggestion"]), json.loads(r["final"])
            gate = next(f for f in HEADS[r["kind"]][1] if f in suggestion)  # the field the gate checked
            band = next(b for lo, hi, b in bands if lo <= r["confidence"] < hi)
            k["human_checked"] += 1
            k["bands"][band]["checked"] += 1
            k["bands"][band]["agreed"] += final.get(gate) == suggestion[gate]
    for k in out.values():
        for b in k["bands"].values():
            b["accuracy"] = round(b["agreed"] / b["checked"], 3) if b["checked"] else None
    return {"threshold": ai.THRESHOLD, "kinds": out}


@router.post("/reviews/{rid}")
def resolve_review(rid: int, fields: dict[str, str] | None = None, db=Depends(get_db),
                   u=role("admin", "supervisor", "qc")):
    """Open item: an empty body accepts the AI's suggestion; fields sent correct it.
    Settled item (auto-applied or already decided): send the fields to correct it."""
    r = one(db, "SELECT * FROM ai_reviews WHERE id = ?", rid)
    if not may_review(u, r["kind"]):
        raise HTTPException(403, f"{r['kind']} decisions are settled by another team")
    if r["status"] != "open" and not fields:
        raise HTTPException(409, "This decision is already settled. Send the corrected fields to change it.")
    allowed = HEADS[r["kind"]][1]
    for f, v in (fields or {}).items():
        if f not in allowed:
            raise HTTPException(422, f"Unknown field '{f}'. Allowed: {', '.join(allowed)}.")
        if v not in allowed[f][1]:
            raise HTTPException(422, f"{f} must be one of: {', '.join(allowed[f][1])}.")
    current = json.loads(r["final"] or r["suggestion"])
    applied = {f: v for f, v in {**current, **(fields or {})}.items() if f in allowed}
    with tx(db):
        apply_decision(db, r["kind"], r["ref_id"], applied)
        db.execute("UPDATE ai_reviews SET status = 'resolved', final = ? WHERE id = ?", (json.dumps(applied), rid))
    return {"ok": True, "applied": applied}
