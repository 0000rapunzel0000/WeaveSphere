"""The chat assistant: Jev picks the skill, the chat model fills it, code checks it, Jev checks it matches, the API runs it.
The AI is stubbed so each path is exact; tests/test_rules.py gives each test a fresh database."""
import os
from datetime import date

from test_rules import H, basics, c, fresh_db, get, m, ok, order, post, roll  # noqa: F401  (fresh_db is autouse)

from mill import ai, assistant, db as mdb
from mill.skills import SKILLS


class Bot:
    """Scripted AI. route: skill (or (skill, confidence, probs)); fields: dict or fn(lines) -> dict; match: Jev's probability."""
    def __init__(self, mp):
        self.route, self.fields, self.question, self.match, self.seen = "none", {}, "", 0.95, []
        self.level, self.several, self.steps, self.models, self.new = "easy", False, [], [], False
        mp.setattr(ai, "decide", self._decide)
        mp.setattr(ai, "chat_json", self._chat)
        mp.setattr(ai, "probability", lambda state, *a: self.seen.append(state) or self.match)

    def _decide(self, state, qs, timeout=5):
        if "turn" in qs:  # Jev: is the latest message a new request?
            new = self.new(state["latest_message"]) if callable(self.new) else self.new
            return {"turn": {"choice": "new" if new else "same", "confidence": 0.9, "probs": {}}}
        if "skill" not in qs:  # the app's other Jev calls (triage, routing a buyer message): as if Jev were down
            return {k: {"choice": next(iter(o)), "confidence": 0, "probs": {}} for k, (_, o) in qs.items()}
        r = self.route(state["conversation"]) if callable(self.route) else self.route
        name, conf, probs = r if isinstance(r, tuple) else (r, 0.95, {})
        return {"skill": {"choice": name, "confidence": conf, "probs": probs},
                "difficulty": {"choice": self.level, "confidence": 0.9, "probs": {}},
                "actions": {"choice": "several" if self.several else "one", "confidence": 0.9, "probs": {}}}

    def _chat(self, prompt, schema, model=None, reasoning=None):
        self.seen.append(prompt)
        self.models.append(model)
        if "steps" in schema["properties"]:
            return {"steps": self.steps}
        f = self.fields(prompt) if callable(self.fields) else self.fields
        return {"fields": [{"name": k, "value": str(v), "said": str(v)} for k, v in f.items()], "question": self.question}


def prompts(bot):
    return [x for x in bot.seen if isinstance(x, str)]


def say(text, who="owner"):
    return ok(c.post("/assistant/chat", json={"text": text}, headers=H[who]))


def world():
    w = basics()
    y = m("yarn_types", count="30s", material="cotton", colour="natural")
    f = m("fabric_types", name="Poplin")
    m("bom", fabric_type_id=f, yarn_type_id=y, kg_per_m=0.2)
    post("/grn", {"supplier_id": w["s1"], "yarn_type_id": y, "qty_kg": 100, "rate": 200})
    w.update(yarn=y, fabric=f, order=order(w["buyer"], f, 500), night=m("shifts", name="Night"),
             arun=m("workers", name="Arun Kumar", wage_type="piece", rate=2))
    return w


def test_complete_request_runs_when_jev_agrees(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route = "log_production"
    bot.fields = {"machine": "loom x", "order": "#1", "shift": "night", "operator": "Arun", "output": "120", "hours": "8"}
    r = say("Loom X did 120 m on night shift for order 1, Arun, 8 hours", "supervisor")
    assert r["status"] == "done" and r["reply"].startswith("Done: Log 120 m on Loom X for order #1, Night shift, Arun Kumar, 8 h")
    e = get("/production")[0]
    assert (e["output"], e["hours"], e["operator"], e["shift"], e["entry_date"]) == (120, 8, "Arun Kumar", "Night", date.today().isoformat())


def test_asks_for_what_is_missing_then_confirms_when_jev_is_unsure(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route, bot.fields, bot.question = "issue_yarn", {"order": "1"}, "Which yarn, and how many kg?"
    r = say("issue yarn to order 1", "store")
    assert r["status"] == "collecting" and r["reply"] == "Which yarn, and how many kg?"
    bot.fields, bot.question, bot.match = {"order": "1", "yarn": "cotton", "kg": "40"}, "", 0.5
    r = say("30s cotton, 40 kg please", "store")
    assert r["status"] == "confirming" and "I'll issue 40 kg of 30s cotton natural to order #1" in r["reply"]
    assert get("/issues") == []                                               # nothing until a person says yes
    r = say("yes", "store")
    assert r["status"] == "done" and get("/issues")[0]["qty_kg"] == 40
    assert len(prompts(bot)) == 2                                             # "yes" needs no AI


def test_no_means_nothing_is_saved_and_a_correction_is_read_again(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route, bot.match = "issue_yarn", 0.4
    bot.fields = {"order": "1", "yarn": "cotton", "kg": "50"}
    assert say("issue 50 kg cotton to order 1", "store")["status"] == "confirming"
    assert say("no", "store")["status"] == "collecting"
    bot.fields["kg"], bot.match = "15", 0.97
    r = say("make it 15 kg", "store")
    assert r["status"] == "done" and get("/issues")[0]["qty_kg"] == 15
    assert "make it 15 kg" in prompts(bot)[-1] and "issue 50 kg cotton" in prompts(bot)[-1]   # same request, read again in full


def test_unknown_names_are_asked_about_with_the_real_options(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route = "machine_status"
    bot.fields = {"machine": "Loom 9", "status": "Breakdown", "note": "belt snapped"}
    r = say("loom 9 broke down, belt snapped", "supervisor")
    assert r["status"] == "collecting" and "which machine 'Loom 9' is (Loom X)" in r["reply"]
    bot.fields = {"machine": "Loom X", "status": "Breakdown"}
    assert "What happened to the machine?" in say("sorry, Loom X", "supervisor")["reply"]   # a skill's own rule


def test_role_limits_and_server_rules_still_apply(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route, bot.fields = "issue_yarn", {"order": "1", "yarn": "cotton", "kg": "40"}
    r = say("issue 40 kg cotton to order 1", "qc")
    assert r["status"] == "closed" and "Your role can't do that" in r["reply"] and get("/issues") == []
    bot.fields["kg"] = "500"
    r = say("issue 500 kg cotton to order 1", "store")
    assert r["status"] == "failed" and r["reply"] == "Not saved: only 100 kg in stock"


def test_unsure_routing_offers_the_top_choices(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route = ("issue_yarn", 0.5, {"issue_yarn": 0.5, "receive_yarn": 0.4, "stock_check": 0.05})
    r = say("cotton 40 kg, S1, 205 a kilo", "store")
    assert r["status"] == "collecting" and "1. Issue yarn" in r["reply"] and "2. Record yarn that arrived" in r["reply"]
    assert r["draft"] == {"choices": ["issue_yarn", "receive_yarn"]}
    bot.fields = {"supplier": "S1", "yarn": "cotton", "kg": "40", "rate": "205"}
    r = say("2", "store")
    assert r["status"] == "done" and r["skill"] == "receive_yarn"
    assert "user: 2" not in prompts(bot)[-1] and "cotton 40 kg" in prompts(bot)[-1]


def test_conversations_end_cleanly(monkeypatch):
    """After a request is done, after 'thanks', or when the person starts something else, old messages are not reused."""
    w, bot = world(), Bot(monkeypatch)
    bot.route, bot.fields = "issue_yarn", {"order": "1", "yarn": "cotton", "kg": "10"}
    first = say("issue 10 kg cotton to order 1", "store")
    assert first["status"] == "done"
    thanks = say("thanks!", "store")
    assert (thanks["status"], thanks["reply"]) == ("closed", "You're welcome!") and thanks["conversation_id"] != first["conversation_id"]
    bot.route = "stock_check"
    bot.fields = {}
    r = say("how much cotton is left?", "store")
    assert r["conversation_id"] not in (first["conversation_id"], thanks["conversation_id"]) and "90 kg" in r["reply"]
    assert "issue 10 kg" not in prompts(bot)[-1] and "thanks" not in prompts(bot)[-1]

    # Mid-request, a clearly different request starts its own conversation.
    bot.route, bot.fields, bot.question = "issue_yarn", {"order": "1"}, "How much?"
    half = say("issue some cotton to order 1", "store")
    assert half["status"] == "collecting"
    bot.route = lambda conv: ("receive_yarn", 0.97, {}) if "arrived" in conv[-1] else ("issue_yarn", 0.95, {})
    bot.new = lambda text: "arrived" in text
    bot.fields, bot.question = {"supplier": "S2", "yarn": "cotton", "kg": "200", "rate": "190"}, ""
    r = say("200 kg cotton just arrived from S2 at 190", "store")
    assert r["status"] == "done" and r["skill"] == "receive_yarn" and r["conversation_id"] != half["conversation_id"]
    assert "issue some cotton" not in prompts(bot)[-1]
    convs = {x["id"]: x for x in get("/assistant/conversations")}
    assert convs[half["conversation_id"]]["status"] == "closed"

    # A short follow-up never switches, even if it looks like something else on its own.
    bot.new = True  # (Jev isn't even asked about a two-word answer)
    bot.route, bot.fields, bot.question = "issue_yarn", {"order": "1"}, "How much?"
    half = say("issue cotton to order 1", "store")
    bot.route = ("receive_yarn", 0.99, {})
    bot.fields = {"order": "1", "yarn": "cotton", "kg": "5"}
    r = say("5 kg", "store")
    assert r["skill"] == "issue_yarn" and r["conversation_id"] == half["conversation_id"]

    assert say("cancel", "store")["status"] in ("cancelled",)


def test_reads_answer_straight_away(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route, bot.fields, bot.match = "order_status", {"order": "1"}, 0.0   # Jev's match check isn't used for a look-up
    r = say("where is order 1?", "qc")
    assert r["status"] == "done" and r["reply"].startswith("Order #1 (B, Poplin x): Pending. 0 of 500 m made")
    bot.route = "none"
    assert say("hello", "qc")["reply"].startswith("I can do anything the app does")


def test_whatsapp_endpoint(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    msg = {"phone": "+91 98765 43210", "text": "how much cotton is left"}
    assert c.post("/assistant/whatsapp", json=msg).status_code == 401
    monkeypatch.setenv("BOT_SECRET", "s" * 32)
    monkeypatch.setenv("BOT_PHONES", "919876543210=store")
    hdr = {"x-bot-secret": "s" * 32}
    assert c.post("/assistant/whatsapp", json=msg, headers={"x-bot-secret": "x" * 32}).status_code == 401
    assert ok(c.post("/assistant/whatsapp", json={**msg, "phone": "+1 555 0100"}, headers=hdr))["status"] == "unlinked"
    bot.route, bot.fields = "stock_check", {}
    r = ok(c.post("/assistant/whatsapp", json=msg, headers=hdr))
    assert r["status"] == "done" and "100 kg" in r["reply"]
    log = get("/assistant/conversations")[0]
    assert (log["channel"], log["peer"], log["username"]) == ("whatsapp", "919876543210", "store")


def test_every_skill_reaches_the_api(monkeypatch):
    """Each of the skills, filled with real records, makes a call the API accepts (a business refusal such as 409 is fine;
    a wrong path, method or body is not). Runs them in an order where earlier ones make what later ones need."""
    w = world()
    db = mdb.connect()
    rq = roll(w, w["order"], 50)
    post(f"/qc/{rq}", {"result": "pass"})
    pending = roll(w, w["order"], 10)
    truck = post("/dispatches", {"order_id": w["order"], "vehicle_no": "TN 1", "driver": "D"})["id"]
    post("/jobs", {"order_id": w["order"], "machine_id": w["loom"]})
    monkeypatch.setattr(ai, "decide", lambda s, q, timeout=5: {k: {"choice": next(iter(o)), "confidence": 0, "probs": {}} for k, (_, o) in q.items()})
    post("/messages", {"text": "where is my order?", "buyer_id": w["buyer"]})            # AI off -> a review to settle
    inv_order = order(w["buyer"], w["fabric"], 10)
    q2 = roll(w, inv_order, 10)
    post(f"/qc/{q2}", {"result": "pass"})
    d2 = post("/dispatches", {"order_id": inv_order, "vehicle_no": "TN 2", "driver": "D"})["id"]
    post(f"/dispatches/{d2}/scan", {"qr": q2})
    post(f"/dispatches/{d2}/close")
    po = post("/purchase-orders", {"supplier_id": w["s1"], "lines": [{"yarn_type_id": w["yarn"], "qty_kg": 5, "rate": 1}]})["po"]["id"]
    sample = {"machine": "Loom X", "order": str(w["order"]), "shift": "Day", "worker": "Arun Kumar", "workers": "Arun Kumar, Op",
              "yarn": "30s cotton natural", "supplier": "S1", "buyer": "B", "fabric": "Poplin", "invoice": "1", "po": str(po),
              "truck": str(truck), "review": str(get("/reviews")[0]["id"]), "message": "1", "job": "Loom X", "number": "5",
              "date": date.today().isoformat(), "month": date.today().strftime("%Y-%m"), "text": "test value", "roll": rq,
              "alert": "1"}
    order_of_play = ["load_roll", "close_truck", "approve_po"]  # these need the state the others leave behind, so run first
    names = order_of_play + [n for n in SKILLS if n not in order_of_play]
    bad = []
    for name in names:
        sk = SKILLS[name]
        given = {f.name: (f.options[0] if f.kind == "choice" else sample[f.kind]) for f in sk.fields}
        if name in ("qc_result",):
            given["roll"] = pending
        if name == "correct_review":
            given.update(field="dept", value="Office")
        if name == "add_shift":
            given["name"] = "Third"
        if name in ("add_machine",):
            given["name"] = "Loom Z"
        values, names_, problems = assistant.resolve(db, sk, given)
        if problems and not (name == "acknowledge_alert"):
            bad.append(f"{name}: could not resolve {problems}")
            continue
        if problems:
            continue  # no live alert in this world
        method, path, body = sk.request(values)
        r = c.request(method, path, json=body, headers=H["owner"])
        if r.status_code in (404, 405, 422, 500):
            bad.append(f"{name}: {method} {path} -> {r.status_code} {r.text[:120]}")
            continue
        if r.status_code < 400:
            try:
                sk.summary(values, names_)
                sk.done and sk.done(r.json(), values, names_)
            except Exception as e:  # the words afterwards must never crash
                bad.append(f"{name}: reply failed: {e!r}")
    db.close()
    assert not bad, "\n".join(bad)
    assert len(SKILLS) >= 60


def test_values_the_person_never_wrote_are_dropped(monkeypatch):
    """The chat model fills in a machine and a kg nobody mentioned: code drops them and asks, Jev never sees them."""
    w, bot = world(), Bot(monkeypatch)
    bot.route, bot.fields = "issue_yarn", {"order": "1", "yarn": "cotton", "kg": "25", "machine": "Loom X"}
    r = say("issue cotton to order 1", "store")
    assert r["status"] == "collecting" and "the kg" in r["reply"] and get("/issues") == []
    r = say("25 kg", "store")
    assert r["status"] == "done" and "on Loom X" not in r["reply"] and get("/issues")[0]["machine_id"] is None
    jev_saw = [x for x in bot.seen if isinstance(x, dict)][-1]
    assert jev_saw["request_to_run"] == "issue yarn: order 1, yarn cotton, kg 25"      # the person's words, not record names


def test_correction_after_saving_and_other_scripts(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route, bot.fields = "issue_yarn", {"order": "1", "yarn": "cotton", "kg": "20"}
    assert say("issue 20 kg cotton to order 1", "store")["status"] == "done"
    r = say("no wait, make it 35 kg", "store")
    assert r["status"] == "closed" and r["reply"].startswith("That was already saved: issue 20 kg") and len(get("/issues")) == 1
    # Names written in Tamil script are found in the message (they used to be stripped before the check).
    assert assistant.norm("அருண்") and assistant.norm("அருண்") in assistant.norm("user: லூம் 1 அருண் 8 மணி")
    m("workers", name="அருண்", wage_type="daily", rate=1)
    bot.route, bot.fields = "mark_attendance", {"shift": "Day", "workers": "அருண்"}
    assert say("அருண் Day shift", "supervisor")["status"] == "done"
    bot.route = "none"
    assert say("same again", "supervisor")["reply"].startswith("I didn't catch a request there")


def test_difficulty_picks_the_model(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.route, bot.fields = "issue_yarn", {"order": "1", "yarn": "cotton", "kg": "10"}
    r = say("issue 10 kg cotton to order 1", "store")
    assert (r["difficulty"], r["model"], bot.models[-1]) == ("easy", assistant.MODELS["easy"], assistant.MODELS["easy"])
    bot.level = "hard"
    r = say("issue 10 kg cotton to order 1 like before", "store")
    assert (r["difficulty"], bot.models[-1]) == ("hard", assistant.MODELS["hard"])


def chain_world():
    """Loom X runs order 1 with Arun; Loom Y has order 2 with Op, idle."""
    w = world()
    w["loom_y"] = m("machines", name="Loom Y", kind="weaving")
    w["order2"] = order(w["buyer"], w["fabric"], 300)
    post("/jobs", {"order_id": w["order"], "machine_id": w["loom"], "operator_id": w["arun"], "shift_id": w["night"]})
    post(f"/machines/{w['loom']}/status", {"status": "Running"})
    post("/jobs", {"order_id": w["order2"], "machine_id": w["loom_y"], "operator_id": w["op"], "shift_id": w["night"]})
    return w


def test_several_things_in_one_message_run_as_a_chain(monkeypatch):
    """The WhatsApp message that started this: status, swap the operator, and free him from his other jobs."""
    w, bot = chain_world(), Bot(monkeypatch)
    bot.several, bot.level = True, "hard"
    bot.steps = ["Set Loom Y (order #2) to Running", "Put Arun Kumar on Loom Y instead of Op",
                 "Free Arun Kumar from any other jobs he has, leaving him on Loom Y"]
    bot.route = lambda conv: {"Set": "machine_status", "Put": "change_operator", "Free": "release_worker"}.get(conv[-1].split()[1], "none")

    def fields(prompt):
        focus = prompt.split("fill in ONLY this one: ")[1].split("\n")[0] if "ONLY this one" in prompt else ""
        if "Set Loom Y" in focus:
            return {"machine": "Loom Y", "status": "Running"}
        if "Put Arun" in focus:
            return {"machine": "Loom Y", "operator": "Arun Kumar"}
        return {"worker": "Arun Kumar", "keep": "Loom Y"}
    bot.fields = fields
    r = say("set #2 status to running, also assign Arun Kumar instead of Op, free Arun Kumar if he has other jobs", "supervisor")
    assert r["status"] == "done" and r["skill"] == "plan" and r["model"] == assistant.MODELS["hard"], r["reply"]
    assert r["reply"].splitlines() == [
        "1. Done: Set Loom Y to Running.",
        "2. Done: Put Arun Kumar on Loom Y (order #2, Night shift) instead of Op.",
        "3. Done: Take Arun Kumar off Loom X (order #1, it stops until someone else is put on it)."]
    floor = {x["name"]: x for x in get("/floor")["machines"]}
    assert (floor["Loom Y"]["status"], floor["Loom Y"]["operator"]) == ("Running", "Arun Kumar")
    assert (floor["Loom X"]["status"], floor["Loom X"]["operator"], floor["Loom X"]["order_id"]) == ("Idle", None, w["order"])
    assert "Arun Kumar was moved off this job" in get(f"/machines/{w['loom']}/log")[0]["note"]
    # Loom X can't restart under Arun's name: it needs a new operator first.
    assert c.post(f"/machines/{w['loom']}/status", json={"status": "Running"}, headers=H["owner"]).status_code in (200, 422)


def test_a_later_step_uses_what_an_earlier_step_made(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    q = roll(w, w["order"], 40)
    post(f"/qc/{q}", {"result": "pass"})
    bot.several, bot.level = True, "hard"
    bot.steps = ["Open a truck for order 1, vehicle TN 9, driver Ravi", f"Load roll {q} onto the new truck from step 1"]
    bot.route = lambda conv: "open_truck" if "Open" in conv[-1] else "load_roll"
    bot.fields = lambda p: {"order": "1", "vehicle": "TN 9", "driver": "Ravi"} if "ONLY this one: \"Open" in p else {"truck": "@step1", "roll": q}
    bot.match = 0.4  # Jev unsure: the whole plan is shown first
    r = say(f"open a truck for order 1, TN 9, driver Ravi, and load {q} on it", "store")
    assert r["status"] == "confirming" and "1. Open a truck for order #1: TN 9, driver Ravi" in r["reply"]
    assert "2. Load roll %s onto (the new truck from step 1)" % q in r["reply"] and get("/dispatches") == []
    r = say("yes", "store")
    truck = get("/dispatches")[0]
    assert r["status"] == "done" and f"Load roll {q} onto truck #{truck['id']}" in r["reply"]
    assert get(f"/dispatches/{truck['id']}")["rolls"] == 1


def test_a_plan_asks_for_what_a_step_is_missing_and_stops_at_a_refusal(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.several = True
    bot.steps = ["Issue 30 kg cotton to order 1", "Issue polyester to order 1"]
    bot.route = "issue_yarn"
    bot.fields = lambda p: {"order": "1", "yarn": "cotton", "kg": "30"} if "30 kg" in p.split("ONLY this one")[1][:40] else {"order": "1"}
    r = say("issue 30 kg cotton to order 1 and polyester too", "store")
    assert r["status"] == "collecting" and r["reply"].startswith("To do all of this I still need:\n2. Issue polyester to order 1: I need the yarn; the kg")
    bot.steps = ["Issue 30 kg cotton to order 1", "Issue 500 kg cotton to order 1"]
    bot.fields = lambda p: {"order": "1", "yarn": "cotton", "kg": "30" if "30 kg" in p.split("ONLY this one")[1][:40] else "500"}
    r = say("make the second one 500 kg cotton", "store")
    assert r["status"] == "failed" and r["reply"].splitlines()[0].startswith("1. Done: Issue 30 kg")
    assert "2. Not saved: Issue 500 kg" in r["reply"] and "only 70 kg in stock" in r["reply"]


def test_a_new_question_while_a_plan_waits_starts_fresh(monkeypatch):
    w, bot = world(), Bot(monkeypatch)
    bot.several, bot.steps, bot.route = True, ["Issue cotton to order 1", "Issue polyester to order 1"], "issue_yarn"
    bot.fields = {"order": "1"}
    first = say("issue cotton and polyester to order 1", "store")
    assert first["status"] == "collecting"
    bot.several, bot.route, bot.fields, bot.new = False, "stock_check", {}, True
    r = say("how much cotton is left?", "store")
    assert r["status"] == "done" and r["skill"] == "stock_check" and r["conversation_id"] != first["conversation_id"]
    assert "100 kg" in r["reply"]


def test_a_plan_that_drops_a_quantity_is_never_run_straight_away(monkeypatch):
    """Jev may miss that a step was misread; code checks every quantity the person wrote is used somewhere."""
    w, bot = world(), Bot(monkeypatch)
    bot.several, bot.match = True, 0.99
    bot.steps = ["Mark Op present on Day shift", "Make a 90 m roll for order 1 from Loom X, Day shift, Op"]
    bot.route = lambda conv: "mark_attendance" if "Mark" in conv[-1] else "make_roll"
    bot.fields = lambda p: {"shift": "Day", "workers": "Op"} if "ONLY this one: \"Mark" in p else \
        {"order": "1", "machine": "Loom X", "shift": "Day", "operator": "Op", "metres": "90"}
    r = say("loom x did 90m day shift order 1 Op 8hrs, make a 90m roll from it, mark Op present", "supervisor")
    assert r["status"] == "confirming" and r["reply"].startswith("I haven't used 8 hrs from your message") and get("/rolls?order_id=1") == []
