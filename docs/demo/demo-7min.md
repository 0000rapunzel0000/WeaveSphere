# 7-minute demo

The features run from most to least important. If you are running long, cut from the bottom: everything above the line
**must show** fits in about 5 minutes. Times are cumulative. Commands, data and roll codes are the same as
[demo-script.md](demo-script.md).

## Setup (before you hit record)

```bash
cd backend && rm -f demo.db demo.db-wal demo.db-shm && DB_PATH=demo.db python3 demo_setup.py
cd backend && DB_PATH=demo.db SIM_SPEED=60 python3 -m uvicorn app:app --port 8000
cd frontend && npx vite --port 5173
cd whatsapp && npm start
cd backend && DB_PATH=demo.db ./voice_dev.sh        # after demo.db is rebuilt, so the tracking links match
```

- **Screen:** browser on the left, signed in as `owner`. WhatsApp Desktop on the right, with the bot chat open.
- **Tabs:** Dashboard; and the order #2 buyer page, already open (Tracking → order #2 → OPEN BUYER PAGE).
- **Clean up:** close the other tabs. Turn on Do Not Disturb. Hide the dock. Use browser zoom at 110–125% so the text reads on video.
- **Headphones on** for the call.

---

## 1. The problem and the dashboard — 0:00 → 0:30

**Show:** Dashboard → *Today at a glance* → the live board.

> "A textile mill runs on notebooks and phone calls. WeaveSphere runs the whole mill in one place: orders, yarn, looms,
> quality, dispatch, money. Every morning Jev, our AI layer, writes this digest from the live numbers. An order is at
> risk, polyester is low, two purchase orders are drafted, and an invoice is overdue. The looms are weaving right now."

## 2. WhatsApp: run the mill by text — 0:30 → 2:00 (the headline)

Send these three messages in WhatsApp Desktop:

1. `Loom 04 did 120 m on night shift for order 1, Arun Kumar, 8 hours` → **Done**, logged.
   > "No app, no form. A supervisor texts what happened, the same way they'd write it in the notebook."
2. `set #5 status to running, also assign Arun Kumar instead of Lakshmi, free Arun Kumar if he has other jobs` →
   a 3-step plan → reply `yes` → 1. Done 2. Done 3. Done.
   > "That's three actions chained in one message. Jev picks the skill and decides how hard the message is, which picks
   > the model. Code checks every name and number against the database. Then Jev checks the plan against what you
   > actually said. If it's sure, it runs; if not, it asks for a YES."
3. `open a truck for order 1, TN 38 AB 1234, driver Selvam, load ETM-1001 and ETM-1002, then close it` →
   4 chained steps, invoice raised.
   > "The truck it opened is the one it loaded and closed. One text, and the invoice exists."

## 3. The app shows what the bot did — 2:00 → 2:40

- **Production:** Loom 02 running with Arun; his old looms note "moved off this job".
- **Assistant:** *What the assistant did*: each request, its level and model, Jev's match score.
  > "Everything goes through the same API and permission rules as the forms. The bot can't do anything your role
  > can't."

## 4. QR traceability and costing — 2:40 → 3:30

**Show:** Traceability → trace `ETM-1003`.
> "Every roll gets a QR label. Scan it and you get the order, the loom, the shift, the operator, the yarn lot, its QC
> result, and what this roll cost to make."

Click **QR LABEL**, then go to **Dispatch** → *Finished goods*: ETM-1003 is flagged as *waiting 12 days*.
> "Nothing gets forgotten in a corner of the godown."

## 5. Buyer page and the AI phone desk — 3:30 → 4:40

**Switch to:** the order #2 buyer tab.
> "Buyers get a link, with no login. They see live progress, and they can call the mill's AI desk."

Tap the round **call** button (Call the order desk), then say:
1. "Hi, where is my order?" → it knows the caller is North Loom Studio and reads order #2's status.
2. "Do I have any payments due?" → their invoices.
3. "What about order one?" → it refuses: that's another buyer's order.
> "The tracking link identifies the buyer, so it only ever talks about their own orders."

Hang up. Keep the call under a minute; it costs credits.

**— must show ends here (~4:40) —**

## 6. Quality with Jev — 4:40 → 5:20

**Show:** Quality → reject `ETM-3001` with the note "holes near the selvedge" → Jev classifies the defect. Then show
*Defect hotspots* (Loom 04 / Night).
> "The inspector writes plain words. Jev turns them into a defect type, and the hotspots show where they come from."

## 7. Orders, yarn and purchasing — 5:20 → 6:00

- **Orders:** order #2 → **YARN** (short of polyester) → **RISK**.
- **Inventory:** the PO draft for ReThread, with its reasoning → **APPROVE**.
> "When an order is booked, it checks the yarn. If stock runs short, a purchase order is drafted with the reason.
> A human approves it."

## 8. Money — 6:00 → 6:25

**Show:** Accounts → *Cost per order* ending in **cost per metre**, the overdue invoice, P&L.

## 9. Roles and close — 6:25 → 6:50

Sign out, then sign in as `qc`: a short menu.
> "Each person sees only their job. The owner adds staff under Workforce. Orders to dispatch to cash, on a phone or a
> laptop, with AI where it saves time and a human where it matters. That's WeaveSphere."

---

## If something fails on camera

- **Bot slow:** say "it's checking the plan" and wait. Each reply takes 3–10 s.
- **Bot asks a question:** answer it. That's a feature ("it asks for anything missing").
- **Call won't connect:** skip to section 6 and say it's shown in the video. `docs/demo/weavesphere-demo.mp4` has it.
