"""Buyer messages and the AI review queue."""
from mill.skills import F, opt, skill

OFFICE = {"admin"}
REVIEWERS = {"admin", "supervisor", "qc"}

skill("log_buyer_message", "Log a message a buyer sent (WhatsApp, email or phone call) into the inbox, so Jev routes it.", OFFICE,
      [F("text", "text", "what the buyer said"), F("buyer", "buyer", "buyer", required=False), F("order", "order", "order", required=False),
       F("channel", "choice", "channel", required=False, options=("whatsapp", "email", "phone"))],
      lambda v: ("POST", "/messages", {"text": v["text"], "buyer_id": v.get("buyer"), "order_id": v.get("order"),
                                       **({"channel": v["channel"]} if v.get("channel") else {})}),
      lambda v, n: f"log a message" + opt(n.get("buyer"), " from {}") + opt(v.get("order"), " about order #{}") + f": \"{v['text']}\"",
      lambda r, v, n: (f"Jev routed it to {r['message']['dept']} ({r['message']['intent'].replace('_', ' ')}, {r['message']['urgency']} urgency)."
                       if r["message"]["status"] == "routed" else "It went to AI review for the office to route."))

skill("buyer_messages", "List buyer messages in the inbox (optionally only those waiting for review, routed or replied).", OFFICE,
      [F("status", "choice", "which ones", required=False, options=("review", "routed", "replied"))],
      lambda v: ("GET", "/messages" + opt(v.get("status"), "?status={}"), None), lambda v, n: "list buyer messages",
      lambda r, v, n: "\n".join(f"#{m['id']} [{m['status']}{opt(m.get('dept'), ' → {}')}] {m['text'][:80]}" for m in r[:10]) or "No messages.",
      read=True)

skill("draft_reply", "Write a reply to a buyer message, from live order data, in English, Tamil, Hindi or Kannada.", OFFICE,
      [F("message", "message", "buyer message"), F("language", "choice", "language", required=False, options=("en", "ta", "hi", "kn"))],
      lambda v: ("POST", f"/messages/{v['message']}/draft", {"lang": v.get("language") or "en"}), lambda v, n: f"draft a reply to message #{v['message']}",
      lambda r, v, n: f"Draft (not sent): {r['draft']}\nTell me 'send it' or give me the text to send.", read=True)

skill("send_reply", "Record the reply that was sent to a buyer message.", OFFICE,
      [F("message", "message", "buyer message"), F("text", "text", "the reply text")],
      lambda v: ("POST", f"/messages/{v['message']}/reply", {"text": v["text"]}), lambda v, n: f"record the reply to message #{v['message']}: \"{v['text']}\"")

skill("ai_reviews", "List the AI decisions waiting for a person to check (review queue).", REVIEWERS, [],
      lambda v: ("GET", "/reviews", None), lambda v, n: "list the review queue",
      lambda r, v, n: "\n".join(f"#{x['id']} {x['kind']}: " + ", ".join(f"{k} {val}" for k, val in x["suggestion"].items()) + f" ({x['confidence']:.0%})"
                                for x in r[:10]) or "Nothing is waiting.", read=True)

skill("accept_review", "Accept an AI decision in the AI REVIEW QUEUE as it is (a review number), e.g. Jev's routing of a buyer message.", REVIEWERS,
      [F("review", "review", "review")], lambda v: ("POST", f"/reviews/{v['review']}", None), lambda v, n: f"accept {n['review']}")

skill("correct_review", "Correct an AI decision: set one field (department, intent, urgency, category, severity, defect type, stage) to the right value.",
      REVIEWERS,
      [F("review", "review", "review"), F("field", "choice", "what to change",
                                          options=("dept", "intent", "urgency", "category", "severity", "defect_type", "stage_at_fault")),
       F("value", "text", "the right value")],
      lambda v: ("POST", f"/reviews/{v['review']}", {v["field"]: v["value"]}), lambda v, n: f"set {v['field']} to {v['value']} on {n['review']}")
