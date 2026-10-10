"""Create or update the Retell buyer-line agent that uses mill/voice.py as its tools.
    python3 voice_setup.py https://<public-url>          # create/update (public URL reaches voice_app on port 8002)
    python3 voice_setup.py --chat "hi" "where is order 2"  # talk to it by text, tools included
Needs RETELL_API_KEY (environment or backend/.env)."""
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

from mill import config  # noqa: F401  loads backend/.env

API = "https://api.retellai.com"
NAME = "Everest buyer line"

PROMPT = """You are the order desk of Everest Textile Mills, a weaving mill. You answer phone calls from buyers
(garment companies and studios) about their orders. You are warm, brief and precise, like a good office manager.

Language: reply in the language the caller uses: English, Tamil or Hindi. Switch if they switch. Follow exactly the same
steps and use the tools in every language. Company names are usually said in English even inside Tamil or Hindi.

How a call goes:
1. At the very start, call identify_caller. If the caller is known, greet them by company name.
2. Before giving any order details, you need the order number. If identify_caller said the caller is not known,
   you also need their company name, passed as company_name. If the caller has already said them, call the tool
   straight away; never ask again for something they already told you.
3. Use order_status for progress, ready dates and trucks. Use invoices_due for payments and dues.
4. If a tool says verified is false, say what it says, and offer that the office calls them back. Never guess.
5. For anything you cannot answer (prices, new orders, quality complaints, changing a date or quantity,
   anything else): say the office will call back today, collect their name and the best number, and confirm the
   request in one sentence. Every call is passed to the office automatically.

Rules:
- Only state facts the tools returned. Never invent dates, numbers, prices or promises.
- Never mention other buyers or other companies' orders.
- Speak numbers naturally: "about six hundred and fifty metres", "the fourteenth of October", "rupees one lakh fifty thousand".
- Stages mean: Pending (not started), Spinning/Weaving/Dyeing (being made), In QC (quality checks), Ready (passed, waiting
  for a truck), Dispatched (fully shipped).
- Keep each turn to one or two short sentences. When the caller is done, thank them and end the call."""

BEGIN = "Vanakkam, Everest Textile Mills order desk. How can I help you today?"

ORDER_ARGS = {"type": "object", "properties": {
    "order_number": {"type": "string", "description": "The order number the caller gives, digits only, e.g. 2"},
    "company_name": {"type": "string", "description": "The caller's company name, if identify_caller said they are not known"}},
    "required": ["order_number"]}


def call(method, path, body=None):
    req = urllib.request.Request(API + path, json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {os.environ['RETELL_API_KEY']}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r) if r.length != 0 else None
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}")


def tools(base):
    tool = lambda name, desc, params, msg: {  # noqa: E731
        "type": "custom", "name": name, "description": desc, "url": f"{base}/voice/retell/tools/{name}",
        "headers": {"X-Voice-Secret": voice_secret()},
        "parameters": params, "speak_during_execution": bool(msg), "speak_after_execution": True,
        **({"execution_message_description": msg, "execution_message_type": "prompt"} if msg else {}),
        "timeout_ms": 10000}
    return [tool("identify_caller", "Look up the caller by their phone number. Call this first, once.",
                 {"type": "object", "properties": {}}, None),
            tool("order_status", "Live status of one of the caller's orders: stage, metres made and passed, ready date, trucks.",
                 ORDER_ARGS, "In the caller's language, say in a few words that you are checking the order."),
            tool("invoices_due", "The caller's unpaid invoices and amounts outstanding (needs one of their order numbers).",
                 ORDER_ARGS, "In the caller's language, say in a few words that you are checking their account."),
            {"type": "end_call", "name": "end_call", "description": "End the call once the caller is done."}]


def voice_secret():
    """Shared secret for the agent's requests (see mill/voice.py signed()); made once and kept in backend/.env."""
    if not os.environ.get("VOICE_SECRET"):
        os.environ["VOICE_SECRET"] = secrets.token_urlsafe(32)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), "a") as f:
            f.write(f"VOICE_SECRET={os.environ['VOICE_SECRET']}\n")
    return os.environ["VOICE_SECRET"]


def find(kind, name):
    return next((a for a in call("GET", f"/list-{kind}") or [] if a.get("agent_name") == name), None)


def setup(base):
    base = base.rstrip("/")
    voices = call("GET", "/list-voices")
    voice = (next((v for v in voices if v["provider"] == "elevenlabs" and "indian" in (v.get("accent") or "").lower()
                   and v["gender"] == "female"), None)
             or next((v for v in voices if "indian" in (v.get("accent") or "").lower()), None)
             or next(v for v in voices if v["provider"] == "elevenlabs"))
    llm_body = {"general_prompt": PROMPT, "begin_message": BEGIN, "general_tools": tools(base), "start_speaker": "agent"}
    agent = find("agents", NAME)
    if agent:
        llm_id = agent["response_engine"]["llm_id"]
        call("PATCH", f"/update-retell-llm/{llm_id}", llm_body)
    else:
        llm_id = call("POST", "/create-retell-llm", llm_body)["llm_id"]
    agent_body = {"agent_name": NAME, "response_engine": {"type": "retell-llm", "llm_id": llm_id},
                  "voice_id": voice["voice_id"], "language": ["en-IN", "ta-IN", "hi-IN"],
                  "webhook_url": f"{base}/voice/retell/webhook?s={voice_secret()}", "webhook_events": ["call_analyzed"],
                  "post_call_analysis_data": [{"type": "system-presets", "name": "call_summary"}],
                  "max_call_duration_ms": 5 * 60 * 1000}  # a forgotten browser tab can't run up minutes
    agent = call("PATCH", f"/update-agent/{agent['agent_id']}", agent_body) if agent else call("POST", "/create-agent", agent_body)
    chat = find("chat-agents", NAME + " (text test)")
    chat_body = {"agent_name": NAME + " (text test)", "response_engine": {"type": "retell-llm", "llm_id": llm_id}}
    if not chat:
        call("POST", "/create-chat-agent", chat_body)
    print(f"agent {agent['agent_id']}  llm {llm_id}  voice {voice['voice_name']} ({voice['provider']}, {voice.get('accent')})")


def chat(lines):
    agent = find("chat-agents", NAME + " (text test)") or sys.exit("run setup first")
    chat_id = call("POST", "/create-chat", {"agent_id": agent["agent_id"]})["chat_id"]
    for line in lines:
        print(f"\nBUYER: {line}")
        for m in call("POST", "/create-chat-completion", {"chat_id": chat_id, "content": line})["messages"]:
            if m["role"] == "agent":
                print(f"AGENT: {m['content']}")
            elif m["role"] == "tool_call_invocation":
                print(f"  -> {m['name']}({m['arguments']})")
            elif m["role"] == "tool_call_result":
                print(f"  <- {m['content'][:300]}")
    call("PATCH", f"/end-chat/{chat_id}")


if __name__ == "__main__":
    if not os.environ.get("RETELL_API_KEY"):
        sys.exit("set RETELL_API_KEY")
    chat(sys.argv[2:]) if sys.argv[1] == "--chat" else setup(sys.argv[1])
