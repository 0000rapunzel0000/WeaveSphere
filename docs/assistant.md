# The chat assistant (web + WhatsApp)

Staff tell the system what happened in their own words — the way they would write it in the notebook — and the
assistant turns that into one checked request, saved through the same API, rules and permissions as the forms.

> "Loom 04 did 130 m on night shift for order 1, Arun, 8 hours" → **Done: Log 130 m on Loom 04 for order #1, Night shift, Arun Kumar, 8 h.**

## How one message is handled

| Step | Who | What |
|---|---|---|
| 1. Pick the skill | **Jev** (1st use) | Chooses which of the 64 skills the conversation is about. Unsure (below `AI_THRESHOLD`) → the person picks from the top 2–3 ("Reply with the number"). |
| 2. Fill the fields | Chat model | Fills only that skill's fields from the conversation, using the mill's real names, and writes down the person's own words for each value. |
| 3. Check | Code | A name or number the person never wrote is dropped (so it is asked for, never made up). Every name is matched to a record (Loom 4 = Loom 04; Loom 01 is never Loom 04). Anything missing or unclear is asked back, with the real options. Skill rules apply (a breakdown needs what happened; a reject needs what is wrong). |
| 4. Does it match? | **Jev** (2nd use) | Compares the conversation with the finished request, written in the person's words. Confident → it runs. Not → "Please check: I'll … Reply YES". Look-ups skip this. |
| 5. Run | The app's API | One call, as the person who is chatting (their sign-in, or on WhatsApp a short-lived token for the linked account). The server's own checks still decide: role, stock, QC-passed rolls, amounts due… |

The reply says what was saved and what followed from it (Jev's breakdown triage, a defect alert, low stock, the invoice total).

## Conversations don't leak into each other

A conversation is one request. It ends when the request is **done**, **fails** or is **cancelled** ("cancel"), on
**"thanks"** (any language), or after **30 quiet minutes**. The next message starts with no history at all.

- Mid-request, a message that is clearly about something else (Jev ≥ 0.6 on another skill and < 10% on this one)
  closes the half-finished request and starts its own. Short follow-ups ("40 kg", "Arun") never do.
- "No wait, make it 35" right after something was saved: the assistant says it was already saved and that entries
  are corrected in the app (nothing is ever deleted from chat).
- "Same for loom 1" after a finished request: it asks for the whole request again rather than guessing.
- "Thanks" while a request waits for YES: it says that request was not saved.

## Skills (`backend/mill/skills/`, one module per area)

| Area | Skills |
|---|---|
| Production | log_production, make_roll, machine_status, assign_job, finish_job, floor_status, machine_history, production_on_day |
| Store & purchasing | receive_yarn (optionally against a PO), issue_yarn, stock_check, low_stock, recent_receipts, purchase_orders, approve_po, cancel_po, change_po_quantity, raise_po, recompute_po_drafts |
| Quality | qc_result, qc_queue, defect_alerts, acknowledge_alert, defect_report, trace_roll |
| Dispatch | open_truck, load_roll, close_truck, trucks, finished_goods |
| Orders | create_order, order_status, order_list, yarn_check, order_risk, tracking_link |
| Inbox & reviews | log_buyer_message, buyer_messages, draft_reply, send_reply, ai_reviews, accept_review, correct_review |
| People | mark_attendance, attendance_on_day, wages |
| Accounts & reports | record_payment, pay_supplier, receivables, payables, profit_and_loss, order_cost, machine_report, morning_digest |
| Setup | add_buyer, add_supplier, add_worker, add_machine, add_yarn, add_fabric, add_recipe, add_shift, set_reorder_level, change_worker_rate |

Not offered on purpose: creating user accounts (passwords don't belong in a chat), printing labels/invoices and the
camera scanner (screen things).

**Adding a skill** is one `skill(...)` call in the right module: a plain description (Jev chooses by it — say what the
person would be asking for), the roles, the fields (each a record kind, number, date, choice or text), how to build the
API call, and the words to reply with. `tests/test_assistant.py::test_every_skill_reaches_the_api` checks every skill's
call against the real API.

## In the app

**Assistant** in the menu (every staff role). Owner and office also see *What the assistant did*: every
conversation from the web and WhatsApp, the skill, the request, Jev's match score and the result.

## WhatsApp (Baileys, demo-sized)

`whatsapp/bot.mjs` links to a WhatsApp account like WhatsApp Web does, forwards each text message to
`POST /assistant/whatsapp` (protected by `BOT_SECRET`) and sends the reply back. The backend does everything else.

1. In `backend/.env` (`BOT_SECRET` is already there):
   ```
   BOT_PHONES=919876543210=floor
   ```
   Each WhatsApp number (country code, digits only) → the mill account it acts as. Several: comma-separated.
   Restart the backend.
2. Start the bridge and scan the QR with the phone: **WhatsApp → Linked devices → Link a device**.
   The QR is also saved as `whatsapp/link-qr.png` and opened, since terminal QRs often don't scan. Or link with a code:
   `PAIR_PHONE=<the bot account's number> npm start`, then on the phone **Link a device → Link with phone number instead**.
   ```
   cd whatsapp && npm install && npm start
   ```
3. Message the linked number from a listed phone. **One phone is enough:** open *Message yourself* on the linked account
   — those messages go to the assistant too (put your own number in `BOT_PHONES`).

An unlisted number gets "This number isn't linked", and the bridge prints the number to add. Delete `whatsapp/auth/` to
unlink. Limits: one linked account, text messages only, no queue — fine for a demo, not for the whole mill.

## Settings

| Variable | Meaning |
|---|---|
| `AI_THRESHOLD` | Jev confidence needed to pick a skill without asking, and to run a request without a YES (demo 0.7) |
| `BOT_SECRET` | Shared secret between the WhatsApp bridge and the backend |
| `BOT_PHONES` | `number=username,…` — which WhatsApp numbers may use the assistant, as whom |
| `PUBLIC_URL` | Base of the buyer tracking links the assistant hands out (default `http://localhost:5173`) |
