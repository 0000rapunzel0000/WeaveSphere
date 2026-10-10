# Alerts, PO drafts and the owner digest

Three things the backend now raises by itself. Code decides and counts; AI only words the digest.
All new endpoints need a login (`Authorization: Bearer <token>`); the errors follow the conventions in backend.md.

## Defect hotspot alerts

After every QC result the backend re-checks that roll's **machine + shift** and its **operator** over the last 30 days
(every QC check counts). It raises an alert when at least 5 checks exist and the failure rate is at least 2x the rate for
the rest of the mill **and** at least 15 points above it. (If the rest of the mill has fewer than 5 checks, it compares
with an assumed 10%.) One live alert per machine+shift or operator; later checks update it, and it clears itself when the
rate returns to normal.

| Method | Path | Notes |
|---|---|---|
| GET | `/alerts/defects?status=live\|cleared\|all` | Default `live` (open + acked). Any role. Each alert has `label`, `checks`, `defects`, `rate`, `baseline`, `top_defect`, `top_stage`, and `suggestion` (the maintenance note). |
| POST | `/alerts/defects/{id}/ack` | admin, qc, supervisor. Status `open` -> `acked`. 409 if already cleared. |
| GET | `/reports/defects?group=machine\|shift\|operator\|machine_shift\|type&days=30` | admin, qc. `{mill, rows}`; rows are worst-first `{label, checks, defects, rate}`; `type` returns `{label, defects, share}`. |

`POST /qc/{qr}` now also returns `defect_alerts`: the alerts that check **newly opened** (usually `[]`). Show a banner when it isn't empty.

## Purchase-order drafts

For each yarn type: `need = kg the open orders still need + reorder level - stock - kg already on order`.
When `need > 0`, a draft PO line is created for the supplier whose latest rate is lowest (looking at the last 10 GRNs).
Drafts are recomputed after every order, GRN and issue, so they track reality. A person's work is never touched:
approved POs, POs raised by hand and lines someone edited all count as "on order" and are left alone.

| Method | Path | Notes |
|---|---|---|
| GET | `/purchase-orders?status=` | `draft`, `approved`, `received`, `cancelled`. Each PO has `lines[]` with `received_kg` and `open_kg`. `received` is derived from GRNs. |
| GET | `/purchase-orders/{id}` | One PO. |
| POST | `/purchase-orders` | admin. `{supplier_id, note?, lines: [{yarn_type_id, qty_kg, rate}]}`. A draft until approved. |
| PUT | `/po-lines/{id}` | admin. `{qty_kg?, rate?}`. Draft only; the system then stops recomputing that line. |
| POST | `/purchase-orders/{id}/approve` | admin. Draft -> approved ("placed with the supplier"). |
| POST | `/purchase-orders/{id}/cancel` | admin. 409 if yarn was already received against it. A cancelled PO's shortfall is redrafted. |
| POST | `/purchase-orders/refresh` | admin, store. Recompute now. Returns `{drafted, skipped, drafts}`; `skipped` lists yarn with no supplier history. |

Receiving: `POST /grn` accepts an optional `po_line_id`. The PO must be approved and the supplier and yarn must match (409 otherwise).
`POST /orders`, `/grn` and `/issues` responses include `po_refresh: {drafted, skipped}`, or `null` if the refresh failed (the main action still succeeds).

## Owner digest

| Method | Path | Notes |
|---|---|---|
| GET | `/digest/today` | admin. Built on the first request of the day, then kept. |
| POST | `/digest/refresh` | admin. Rebuild now. |

Returns `{date, summary, source, facts, created_at}`. `facts` are plain code: yesterday's output, low stock, PO drafts waiting,
orders at risk (rule nominates, Jev confirms), machines not running, overdue receivables, open defect alerts, reviews waiting.
`summary` is a short paragraph; `source` is `ai` or `template`. If the chat model writes any number that is not in `facts`,
the text is discarded and the template is used. Render the facts as cards; show `summary` as the headline.

## Settings

| Variable | Default | |
|---|---|---|
| `DEFECT_DAYS` | 30 | How far back QC checks count |
| `DEFECT_MIN_CHECKS` | 5 | Fewer checks than this never alerts |
| `DEFECT_MULT` / `DEFECT_POINTS` | 2 / 0.15 | Times the rest-of-mill rate, and minimum gap in rate |
| `CREDIT_DAYS` | 30 | An invoice is overdue this many days after it is issued |
| `DIGEST_HOUR` | unset | e.g. `7` builds the digest at 07:00 server time every day. Unset = built on first request. |
