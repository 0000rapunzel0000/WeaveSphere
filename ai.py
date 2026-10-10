"""Jev for decisions, Gemini for words. Never used for numbers."""
import json
import logging
import os
import urllib.request

THRESHOLD = float(os.environ.get("AI_THRESHOLD", "0.8"))
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _gemini(parts, schema=None):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    cfg = {"responseMimeType": "application/json", "responseSchema": schema} if schema else {}
    out = _post(GEMINI_URL.format(os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")),
                {"contents": [{"parts": parts}], "generationConfig": cfg},
                {"x-goog-api-key": key}, timeout=30)
    text = out["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text) if schema else text


def classify(state: dict, options: list[str]) -> dict:
    """-> {choice, probs, confidence}. Jev if JEV_URL is set, else Gemini. If both fail,
    probs come back uniform, so confidence is low and the caller routes to human review."""
    probs = {}
    try:
        if os.environ.get("JEV_URL"):
            # ponytail: assumes Jev answers {"probs": {option: p}}; adapt here once its API is fixed.
            probs = _post(os.environ["JEV_URL"], {"state": state, "options": options},
                          {"Authorization": f"Bearer {os.environ.get('JEV_KEY', '')}"}, timeout=2)["probs"]
        elif os.environ.get("GEMINI_API_KEY"):
            prompt = ("Classify this textile-mill situation. Give a probability (0 to 1, summing to 1) "
                      f"for each option.\nState: {json.dumps(state, default=str)}\nOptions: {options}")
            schema = {"type": "OBJECT", "properties": {o: {"type": "NUMBER"} for o in options}, "required": options}
            probs = _gemini([{"text": prompt}], schema)
    except Exception:
        logging.exception("classify failed, sending to human review")
    probs = {o: max(0.0, float(probs.get(o) or 0)) for o in options}
    total = sum(probs.values())
    probs = {o: p / total if total else 1 / len(options) for o, p in probs.items()}
    choice = max(probs, key=probs.get)
    return {"choice": choice, "probs": probs, "confidence": probs[choice]}


LANGS = {"en": "English", "ta": "Tamil", "hi": "Hindi"}


def draft_reply(message: str, order_data: dict, lang: str) -> str:
    return _gemini([{"text": (
        f"You are the office of Everest Textile Mills. Draft a short, polite WhatsApp reply in {LANGS[lang]} "
        "to the buyer's message, using ONLY the live order data below. Do not invent dates or numbers.\n"
        f"Buyer message: {message}\nOrder data: {json.dumps(order_data, default=str)}")}]).strip()


def read_challan(image_b64: str, mime: str) -> dict:
    schema = {"type": "OBJECT", "properties": {
        "supplier": {"type": "STRING"}, "challan_no": {"type": "STRING"},
        "yarn_count": {"type": "STRING"}, "material": {"type": "STRING"}, "colour": {"type": "STRING"},
        "qty_kg": {"type": "NUMBER"}, "rate": {"type": "NUMBER"}}}
    return _gemini([{"inline_data": {"mime_type": mime, "data": image_b64}},
                    {"text": "Extract the yarn delivery challan fields. Leave a field out if unreadable."}], schema)
