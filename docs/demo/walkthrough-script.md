# Walkthrough voice-over script (6:07)

Video: `docs/demo/weavesphere-walkthrough.mp4`, 2560 × 1440, 30 fps, no sound. It's a real screen recording of the app
on fresh demo data, with no effects. Waits on the AI are cut out, so every answer appears at once.

Each line is timed to what is on screen, at an easy pace of about 2.3 words a second. **Start each block at its
timecode.** If you finish early, let the picture run; don't stretch the words. Bold text is what appears on screen.

---

### 0:00 – 0:12 · Landing
> This is WeaveSphere, the operating system for Everest Textile Mills. One system follows an order from yarn at the gate
> to the truck at the dock.

### 0:12 – 0:17 · Sign in
> Every person signs in with their own role.

### 0:17 – 0:35 · Dashboard
> The owner starts the day here. *Today at a glance* is written by AI from live numbers: orders at risk, low stock,
> waiting purchase orders, overdue money. *(0:24, machine board)* Below is the floor, live: each loom, its order,
> its progress and who is on it.

### 0:35 – 1:00 · Orders
> A new order: Meridian Apparel, organic poplin, 500 metres at 480 rupees. When we save it, the system checks the yarn
> recipe against stock straight away. *(0:53, RISK on order 2)* For any order we can ask Jev, our AI layer, for a
> deadline estimate. Order 2 is **at risk, 65 per cent**: it's half done, with yarn short and two idle machines.

### 1:00 – 1:25 · Inventory
> Stock is live: receipts minus issues. Polyester is flagged low. *(1:06)* The system drafts purchase orders on its own,
> from what open orders still need, and shows its reasoning. *(1:11)* We approve one, *(1:16)* then book the
> delivery against it with the supplier's challan number. Stock goes up immediately.

### 1:25 – 2:07 · Production
> The production floor. *(1:28)* Running looms produce at their set speed, so metres and yarn use update live.
> *(1:36)* Loom 04 breaks down. The supervisor types what happened in plain words, *(1:43)* and Jev triages it:
> **mechanical, medium, send the fitter**. *(1:48)* Then a shift entry: Loom 01, order 2, Meena Devi, 160 metres,
> and make a roll from it. *(1:57)* Every roll gets its own QR label, ready to print and stick on the roll.

### 2:07 – 2:39 · Quality
> Quality control. Rolls wait here for inspection. *(2:11)* One click passes a roll. *(2:15)* For a reject, the
> inspector just writes what they saw, *(2:23)* and Jev classifies it: **hole, a weaving fault, medium severity**.
> *(2:29)* The system also watches failure rates. When one operator or one loom on one shift fails far more often
> than the rest of the mill, it raises a hotspot with what to do next.

### 2:39 – 2:59 · Traceability
> Any roll can be traced from its QR code. ETM-1003 comes from order 1, organic poplin, woven on Loom 04 on the night
> shift by Arun Kumar. *(2:48)* We see the yarn issued, its QC history, and what this roll cost to make. *(2:54)* The
> label can be reprinted at any time.

### 2:59 – 3:38 · Dispatch
> Finished goods. *(3:01)* A roll has sat on the shelf for **12 days**, so it is flagged before anyone forgets it.
> *(3:07)* We open a truck for order 1 and load the passed rolls with one tap each. *(3:17)* We try a rejected roll:
> **blocked**. Only rolls that passed QC can ship. *(3:25)* Closing the truck raises the GST invoice from the packing
> list in the same step.

### 3:38 – 3:57 · Accounts
> Accounts. Revenue, receivables, payables and profit for the month. *(3:45)* Every order is costed from yarn, labour
> and machine hours, down to the **cost per metre**. A roll costs its metres times that rate. *(3:52)* Receivables
> show who owes what.

### 3:57 – 4:22 · Inbox
> Buyer messages arrive by WhatsApp, email or phone. North Loom asks when their navy twill will be ready.
> *(4:05)* Jev routes it: production, a status question, with its confidence. *(4:12)* And it drafts the reply from
> live order data, here in **Tamil**. A person checks it before it is sent.

### 4:22 – 4:32 · Reviews
> Whenever Jev isn't confident, the decision waits here for a person. Their corrections are kept, so accuracy is
> measured.

### 4:32 – 5:14 · Assistant
> The assistant does the same work in plain words, on this page or on WhatsApp. One message, three jobs: set order 5
> running, put Arun on it instead of Lakshmi, and free Arun from his other looms. *(4:38)* Jev picks the skill and splits
> the message into steps. Code checks every name and number. *(4:42)* Jev wasn't sure the plan matched, so it asks for
> a YES. *(4:49)* Confirmed: all three are done, through the same rules as the forms. *(4:57)* Questions work too.
> *(5:07)* Every request is logged with its skill, the model used, and Jev's match score.

### 5:14 – 5:38 · Workforce
> Workforce: the attendance register for the month, and wages from daily or piece rates. *(5:23)* The owner or office
> adds staff accounts here, with a role, email and phone. Each role sees only its own screens.

### 5:38 – 5:54 · Buyer page
> Buyers get a private tracking link, with no login needed. They see their order's stage, metres made and passed, and
> the expected ready date. *(5:44)* With one tap they can talk to our AI order desk, which knows exactly which order
> they are calling about.

### 5:54 – 6:07 · Roles
> And when the QC inspector signs in, they get a short menu with only their work. That's WeaveSphere: one connected
> system, from yarn to truck. Thank you.

---

**Word count:** about 830 words, which fits 6:07 at a relaxed pace. If you need more breathing room, the tightest
blocks are 0:12 (sign in), 4:22 (reviews) and 5:54 (close). Drop their last sentence first.

**Re-recording:** `docs/demo/record_walkthrough.py` drives the app on a fresh demo database; its docstring has the
steps. Each take changes the data, so rebuild the database before every take.
