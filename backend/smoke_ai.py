"""Live check against OpenRouter (spends a few cents). Run:
    cd backend && OPENROUTER_API_KEY=... python3 smoke_ai.py [challan.jpg]
"""
import base64
import logging
import os
import sys
import tempfile
import time

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")  # importing the app must not touch mill.db
from mill import ai  # noqa: E402
from mill.reviews import HEADS  # noqa: E402  the exact questions and option descriptions the app sends

logging.basicConfig(format="   ! %(message)s")
if not os.environ.get("OPENROUTER_API_KEY", "").startswith("sk-or-v1-"):
    sys.exit("set OPENROUTER_API_KEY to your real key (starts with sk-or-v1-)")


def timed(label, fn):
    t = time.perf_counter()
    try:
        out = fn()
    except Exception as e:
        out = f"FAILED: {e!r}"
    print(f"\n== {label} ({(time.perf_counter() - t) * 1000:.0f} ms)\n{out}")
    return out


QS = {f: q for f, q in HEADS["message"][1].items()}
order = {"order_no": 12, "fabric": "Poplin", "colour": "navy", "stage": "Weaving", "target_m": 5000,
         "passed_m": 1500, "deadline": "2026-10-20", "expected_ready": "2026-10-24"}
clear = timed("Jev: clear buyer message", lambda: ai.decide(
    {"text": "Hi, when will order 12 be ready? Our cutting starts on the 18th.", "orders": [order]}, QS))
timed("Jev: ambiguous buyer message", lambda: ai.decide({"text": "hmm ok", "orders": []}, QS))
timed("Jev: breakdown triage", lambda: ai.decide(
    {"note": "yarn keeps snapping on the warp, lots of fluff", "machine": "Loom 4", "kind": "weaving"},
    HEADS["breakdown"][1]))
timed("Jev: QC defect", lambda: ai.decide(
    {"note": "small holes every few metres along the selvedge", "machine": "Loom 4", "machine_kind": "weaving"},
    HEADS["defect"][1]))
timed("Jev: order risk (noul)", lambda: ai.probability(
    {"progress_pct": 30, "days_left": 4, "yarn_short_kg": 120, "free_machines": 0},
    "Will this order miss its deadline?", "the order is likely to finish after its deadline", "the order will finish on time"))
timed("Chat: reply draft (Tamil)", lambda: ai.draft_reply("when will order 12 be ready?", order, "ta"))
if len(sys.argv) > 1:
    img = base64.b64encode(open(sys.argv[1], "rb").read()).decode()
    timed("Chat: challan photo", lambda: ai.read_challan(img, "image/png" if sys.argv[1].endswith(".png") else "image/jpeg"))

ok = isinstance(clear, dict) and all(a["confidence"] > 0 for a in clear.values())
print("\nJev OK" if ok else "\nJev returned nothing usable: check the key, credits, and the request format in mill/ai.py")
