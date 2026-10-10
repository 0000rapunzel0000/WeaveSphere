# WeaveSphere — functional test plan

Goal: before the functionality review, prove that every feature in `docs/spec.md` does what the spec says, with the
numbers worked out by hand, not read back from the app. The voice desk is out of scope.

**Ground rules**
- Tests never touch your servers (8000 / 5173), `mill.db` or `demo.db`. They use temporary databases, copies, and
  their own ports (backend 8005, frontend 5176).
- Every expected number below is worked out from the rule, so a wrong formula fails the test.
- A failure gets a root-cause fix and a test that would have caught it, then the affected layer is run again.
- Done means: every layer passes, and `demo.db` is rebuilt fresh and checked.

## Layers

| # | Layer | What it proves | How |
|---|---|---|---|
| A | Existing suite | Main flow, hardening, live floor, race on the last kg, PO drafts, hotspots, audit fixes, scanned links, register | `pytest tests/test_app.py` (AI off) |
| B | Rules suite (new) | Each business rule's arithmetic and edge cases, the permission matrix, the AI gate | `pytest tests/test_rules.py` (AI off, Jev stubbed where needed) |
| C | Live AI | Jev and Gemini really answer, on the demo data | script against a copy of `demo.db` with the real key |
| D | Browser | Every role × page × desktop/phone loads clean; the evaluator walkthrough works click by click | Playwright against a copy of `demo.db` |
| E | Demo data | `demo.db` rebuilt fresh, and has everything the walkthrough needs | `demo_setup.py` and a state check |

## B. The rules, and how each is checked

Fresh database, one small mill built for the test (yarns Y1, Y2; fabrics F1 = 0.2 kg/m Y1, F2 = 0.1 kg/m Y1 + 0.3 kg/m Y2).

**B1. Stock and moving-average rate.** Stock = all receipts − all issues (a view, never stored).
Rate = (value received − value issued) ÷ kg on hand. An issue is costed at the rate at that moment.
- Receive 500 kg @ ₹200, then 500 kg @ ₹220 → 1,000 kg @ ₹210.
- Issue 100 kg → costed @ ₹210; 900 kg left, still @ ₹210.
- Receive 100 kg @ ₹150 → 1,000 kg, value 189,000 + 15,000 → @ ₹204.
- Issuing more than is on hand → refused (409). Low-stock list = yarns with stock ≤ reorder level (reorder > 0).

**B2. Yarn feasibility (first come, first served).** Need = metres × kg/m per recipe yarn. Free stock = stock − what
earlier open orders still need (their need − what was issued to them). Short = need − issued − free.
- Y2 stock 100 kg. Order A (F2, 200 m) needs Y2 60 kg → can start. Order B (F2, 200 m) needs 60, A reserves 60 → free 40 → short 20.
- Issue A its 60 kg → stock 40, A reserves 0 → B still short 20 (issuing moves yarn, it doesn't create any).
- A fabric with no recipe → "no BOM for this fabric type", can't start.

**B3. Purchase-order drafts.** Need = open-order demand still un-issued + reorder level − stock − on order.
"On order" = approved, manual or person-edited lines not yet received; the system's own unedited drafts are recomputed.
Supplier = lowest latest rate over the last 10 receipts of that yarn.
- Y2 with reorder 0: demand 120 (A 60 + B 60), stock 100 → draft 20 kg from the only supplier, at its rate.
- A cheaper delivery from supplier 2 (10 kg @ lower rate) → need 10, and the draft moves to supplier 2.
- A person edits the line → it is kept (locked) and counts as on order; new demand goes on a new draft line.
- Approve → no longer a draft; edit after approval → 409; approve twice → 409.
- GRN against a draft line → 409; wrong supplier → 409; full delivery → PO shows "received".
- Cancel after yarn arrived → 409; cancel an unreceived approved PO → the need is drafted again.

**B4. Order stage (derived, never typed).** Dispatched (shipped ≥ ordered) › Ready (passed ≥ ordered) › In QC (rolls
waiting/rework) › furthest job-card stage (Dyeing › Weaving › Spinning) › Pending. Rejected rolls count nowhere.
- New order → Pending; job on a loom → Weaving; also on the dye line → Dyeing; a roll waiting → In QC;
  rejected roll → back to Dyeing; passed ≥ ordered → Ready; part shipped → not Dispatched; all shipped → Dispatched.

**B5. Expected ready date.** Rate = last 7 days' output on the furthest fabric stage ÷ active days.
Ready = today + ceil(remaining ÷ rate). Nothing logged in 7 days → no date.
- 100 m order, 30 m + 10 m logged on two days this week → 20 m/day → 60 m left → today + 3 days.

**B6. Live floor.** Covered by layer A (speed × time, issued yarn first then store, stop when out, finish closes the
job, wages and overhead). Added here: the demo clock — at SIM_SPEED > 1, a restart doesn't fast-forward downtime.

**B7. QC and defect hotspots.** Pass needs nothing; rework/reject need a defect type or notes. A checked roll can't be
checked again unless it's in rework. Hotspot: ≥ 5 checks in 30 days and rate ≥ max(2 × rest-of-mill, rest + 15 points);
rest-of-mill uses 10% when it has < 5 checks.
- Rest of mill 5 checks / 1 fail = 20%. Hotspot needs ≥ 40%. Loom X: 5 checks, 2 fail (40%) → alert opens.
- Pass rolls until the rate drops below 40% → alert clears. Ack a cleared alert → 409.
- Defect report by machine / shift / operator / machine+shift / type: counts and rates match the checks.

**B8. Dispatch and invoice.** Truck must be open; roll on one truck only; same order; passed QC.
Invoice = metres × order price; GST = subtotal × fabric GST %; total = subtotal + GST (rounded to paise).
- 2 rolls × 75.5 m @ ₹333.33, GST 12% → subtotal 50,332.83, GST 6,039.94, total 56,372.77.
- Close an empty truck → 409; close twice → 409; scan onto a closed truck → 409.

**B9. Payments, receivables, payables, P&L.** Buyer payment must name an invoice of that buyer and can't exceed
what's due. Payables = supplier's GRN value − payments to it. P&L month = invoice subtotals − yarn issued − wages −
machine overhead in that month. Machine report counts that month's breakdowns.

**B10. Wages and costing.** Daily: rate × distinct days present (a double shift is one day). Piece: rate × output.
Order cost = yarn issued at its rates + labour (piece output × rate; daily rate split by hours that day) + hours × overhead.

**B11. AI gate.** The first question's confidence decides: ≥ threshold → applied (and logged as resolved); below →
review queue. A message Jev can only call "other" always goes to a person. An answer outside the options = confidence 0.
Breakdown assignee follows from the category by rule. Accuracy report bands by confidence.
- Confidence exactly 0.8 → applied; 0.79 → queued; "other" at 0.99 → queued; invalid choice → queued.

**B12. Digest.** Facts by code: overdue = outstanding > 0 and issued more than 30 days ago; low stock; PO drafts;
idle machines; open alerts; open reviews. If the model's text has any number not in the facts → template used.

**B13. Buyer link.** Shows one order, and no prices, buyer details or other orders. Wrong token → 404.

**B14. Who may do what (server side).** Every endpoint called by every role; allowed ↔ spec section 2. Owner may do
anything; admin can't create an owner; reviews are filtered to the team that owns each kind.

**B15. Input safety.** Unknown master table or field → 404/422; machine status can't be set through Masters; a record
pointing at something that doesn't exist → 409 with a plain message; errors are always one readable line.

## C. Live AI (on a copy of demo.db, real key)
Message routing (a clear status question → Production; "ok noted" → review), breakdown triage with assignee,
defect classification from notes, deadline risk returns a probability, reply drafts in English, Tamil and Hindi that
use only real order numbers, the challan photo fills supplier/yarn/kg/rate, and the digest's AI text passes the number check.

## D. Browser
1. **Role sweep:** each of the 5 roles, desktop and phone: the menu matches spec section 2 exactly, and every page
   loads with no script errors, no failed API calls, no error notices and no sideways scrolling.
2. **Evaluator walkthrough** (the order you'll demo, on a copy of `demo.db` at SIM_SPEED 60): landing → sign in →
   dashboard digest and live board → orders (YARN, RISK, new order) → inventory (approve PO, GRN against it, challan
   photo, issue yarn) → production (metres rising, breakdown + triage, shift output + roll) → quality (pass, reject +
   classification, hotspots, WE'RE ON IT) → traceability (trace, label, print, scan) → dispatch (truck, load, blocked
   roll, close + invoice) → accounts (payment, overdue order #4) → inbox (log, Tamil draft, mark sent) → reviews
   (settle both) → workforce (attendance, register, previous month) → buyer page on a phone → reports → masters → sign out.

## E. Demo data
Rebuild `demo.db`, then check: 5 orders in the expected stages, Loom 01 and Loom 04 running, rolls waiting for QC,
passed rolls for a truck, 2 PO drafts, a live hotspot, reviews waiting, order #4's invoice overdue, 3 weeks of attendance.

## Results (run 2026-10-11)

| Layer | Result |
|---|---|
| A. Existing suite | 9 / 9 pass |
| B. Rules suite | 12 / 12 pass (77-endpoint × 5-role permission matrix included) |
| C. Live AI | Routing (Production, Office/new order, "ok noted" → review), triage → fitter, defect → hole/weaving, risk 0.76 for rush order #5, drafts in EN / TA / HI quoting real dates and metres, an unknown fabric (denim) answered honestly, challan read (South Cotton, 500 kg @ ₹212), digest worded by AI and passing the number check |
| D1. Role sweep | 100 / 100 pages clean (5 roles × desktop and phone) |
| D2. Walkthrough | 52 / 52 steps pass, no console errors, no failed requests |
| E. Demo data | `demo.db` rebuilt; 5 orders in the expected stages, Loom 01 and 04 running, 5 rolls for QC, 3 for a truck, 2 PO drafts, 2 hotspot alerts, 2 reviews, order #4 overdue |

**Bugs found and fixed**
1. Editing a PO draft line down left the rest of the shortfall undrafted until some unrelated receipt or issue happened
   (the screen was wrong in the meantime). Drafts are now recomputed straight after an edit. Test: `test_purchase_order_drafts`.
2. Accounts → payment: the invoice list shows every buyer's invoices, so picking an invoice of a buyer other than the one
   selected was refused by the server ("invoice does not belong to this buyer"). Picking an invoice now selects its buyer.
   Checked in the walkthrough ("picking the invoice selects its buyer").

## Chat assistant (run 2026-10-11)

| Check | Result |
|---|---|
| `tests/test_assistant.py` (AI stubbed: every path exact) | 12 / 12 — complete request, follow-up questions, YES/NO, corrections, unknown names, role limits, server refusals, unsure routing, conversation boundaries, grounding, WhatsApp secret and number links, **every one of the 64 skills accepted by the real API** |
| Jev routing, one real message per skill (live) | 64 / 64 correct |
| Live conversations on demo data (English, Tamil script, Tanglish; Hindi routing) | Requests saved with Jev match 0.70–0.97; wrong requests stopped (0.02–0.21) |
| Browser: Assistant page, desktop + phone, office log | 9 / 9, no errors |
| Role sweep with the new page | 110 / 110 |
| Walkthrough regression | 52 / 52 |
| WhatsApp bridge | connects and shows the link QR; bridge requests answered; wrong secret 401; unlinked number refused |

Bugs found while building it, fixed and tested: near-spelling matching could turn "Loom 9" into "Loom X" (now numbers
must be identical); a bare "loom" picked one loom (now asks which); names in Tamil script were stripped before the
check; the chat model added a date nobody said (now dropped by code, and Jev scored it 0.21).

## Chains and model tiers (run 2026-10-11)

| Check | Result |
|---|---|
| Unit tests (AI stubbed) | 39 / 39 — chains, a step using an earlier step's result, per-step questions, stop at first refusal, dropped-quantity guard, same/new request switch, tier → model |
| Jev routing, one message per skill (live) | 66 / 66 |
| Jev "same request or new?" (live, 11 hand-made cases incl. Tanglish) | 11 / 11 |
| Live chains on demo data | the original WhatsApp message → correct 3-step plan (shown for YES); truck open + 2 loads + close; new order + issue to it; output + roll + attendance; Tanglish output + breakdown — all correct, ~6 s each |

Found and fixed: the pro model took 28 s to split one message (now 3.7-flash, low thinking: ~2.5 s); a wrongly routed
step slipped past Jev's plan check (now code also checks every quantity is used); "start both looms" read as "start an
order"; a new message stuck to a plan still waiting for YES (now Jev judges same/new from three focused fields); a job with
its operator removed would have been credited to the last person who ran the machine (now it can't run until reassigned);
parallel steps shared one database connection.

Not a bug, noted: the Reviews page lists auto-applied decisions as well as waiting ones (they can still be corrected),
so it shows more rows than the "AI reviews" count on the dashboard, which counts only the waiting ones.
