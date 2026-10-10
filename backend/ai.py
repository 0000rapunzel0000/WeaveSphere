"""Everything goes through OpenRouter: Jev for decisions, a chat model for words. Never used for numbers."""
import json
import logging
import os
import urllib.request

THRESHOLD = float(os.environ.get("AI_THRESHOLD", "0.8"))
JEV_MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "google/gemini-2.5-flash")  # needs vision for challan photos


def _post(path, body, timeout):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    req = urllib.request.Request("https://openrouter.ai/api" + path, json.dumps(body, default=str).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def decide(state, questions: dict[str, tuple[str, dict[str, str]]]) -> dict[str, dict]:
    """Jev choice questions, all in one call: {name: (instructions, {option: description})}
    -> {name: {choice, probs, confidence}}.
    If Jev fails, or answers outside the options, confidence is 0 so the caller sends it to human review."""
    try:
        answers = _post("/alpha/decisions", {"model": JEV_MODEL, "state": state, "questions": {
            name: {"type": "choice", "instructions": instr, "criteria": opts}
            for name, (instr, opts) in questions.items()}}, timeout=5)["answers"]
    except Exception as e:
        logging.warning("Jev failed, sending to human review: %r", e)
        answers = {}
    out = {}
    for name, (_, opts) in questions.items():
        a = answers.get(name) or {}
        ok = a.get("choice") in opts
        out[name] = {"choice": a["choice"] if ok else next(iter(opts)), "probs": a.get("probabilities", {}),
                     "confidence": float(a.get("confidence") or 0) if ok else 0.0}
    return out


def probability(state, instructions: str, true: str, false: str) -> float | None:
    """Jev yes/no question -> probability of `true`, or None if Jev is unavailable."""
    try:
        return float(_post("/alpha/decisions", {"model": JEV_MODEL, "state": state, "questions": {
            "q": {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}}},
            timeout=5)["answers"]["q"]["noul"])
    except Exception as e:
        logging.warning("Jev failed: %r", e)
        return None


def _chat(content, schema=None):
    body = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": content}]}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
    text = _post("/v1/chat/completions", body, timeout=60)["choices"][0]["message"]["content"]
    return json.loads(text) if schema else text.strip()


LANGS = {"en": "English", "ta": "Tamil", "hi": "Hindi"}


def draft_reply(message: str, order_data: dict, lang: str) -> str:
    return _chat(f"You are the office of Everest Textile Mills. Draft a short, polite WhatsApp reply in {LANGS[lang]} "
                 "to the buyer's message, using ONLY the live order data below. Do not invent dates or numbers. "
                 "Reply with the message text only.\n"
                 f"Buyer message: {message}\nOrder data: {json.dumps(order_data, default=str)}")


def read_challan(image_b64: str, mime: str) -> dict:
    schema = {"type": "object", "properties": {
        "supplier": {"type": "string"}, "challan_no": {"type": "string"},
        "yarn_count": {"type": "string"}, "material": {"type": "string"}, "colour": {"type": "string"},
        "qty_kg": {"type": "number"}, "rate": {"type": "number"}}}
    return _chat([{"type": "text", "text": "Extract the yarn delivery challan fields. Leave a field out if unreadable."},
                  {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}}], schema)
