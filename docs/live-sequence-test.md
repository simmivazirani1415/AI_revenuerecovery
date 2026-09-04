# Live multi-turn sequence — INV-23 (Brightline Retail)

A full negotiation on a single invoice, played out over live WhatsApp turn by
turn, exercising the real pipeline: `decide` → `draft` → `guardrail` → send, the
inbound webhook, re-diagnosis, promise capture, promise-broken + tier recompute,
and a contact-cap escalation.

**Invoice:** INV-23 — Brightline Retail, ₹300,000, 15 days past terms, **amber**,
standard segment, owner **faiz**. Baseline diagnosis `cash_stress` (reply on file:
*"We're facing a cashflow crunch this quarter."*). Contact cap for standard = **3**.

**Test conditions**
- **Delivery:** tried `body=` first to deliver the real drafted text (24h window
  open). The account rejected it with **21654 "ContentSid Required"**, so delivery
  fell back to the approved template `HX7cf5…`, which has **zero variable slots** —
  so the phone shows a generic stub. Each turn shows the **real `draft()` message**
  next to the **delivered stub** so the gap is visible.
- **Bypassed for this test only** (`policy.py` untouched): quiet hours and the 48h
  inter-contact gap. Every other guardrail — hard stops, open-promise, contact cap
  — is honored for real.

---

## Turn 1 — First contact (outbound)

**State:** diagnosis `cash_stress`, contacts 0, no promise, tier amber.

**Decision:** `offer_payment_plan` · register **warm** · rule `table: cash_stress →
offer payment plan` · owner faiz. Guardrail: would block on `quiet_hours` →
bypassed; else passed.

**Real draft (`draft()`, warm):**
```
[Brightline Retail] Hi Brightline Retail team, samajhte hain quarter thoda tight hai.
Hum invoice INV-23 (Rs 300,000) ko instalments mein baant sakte hain - bata dijiye
kya suit karta hai. Pay here: https://rzp.io/rzp/vrnPlf3
```
**Delivered (stub, after 21654 fallback):**
```
Hi, this is Support. We received your message and will assist shortly. Reply with
details. Test message from Twilio.
```
Sent · SID `MMc77a7c…1ef1` · path `template` · contact #1.

---

## Turn 2 — Reply 1 → promise captured → agent waits

**Client said** (inbound event 986, live webhook):
> *"we've had a delay from our own client, give us till Friday"*

**Re-diagnosis:** `cash_stress` → **`approver_bottleneck`** (method llm/gemini,
trigger `inbound_reply`) — read "waiting on our own client" as an external
sign-off/approval bottleneck; no cash keyword in this reply.

**Promise captured:** `promise_status=pending`, `promise_date=2026-09-11` (Friday).

**Decision:** `do_nothing` · rule `override 3: open promise not yet due → wait` ·
routed_reason *"promise pending until 2026-09-11 — wait it out."* **No contact
sent** — a pending promise is a hard-stop and an override to hold. The agent
respects the commitment and waits.

---

## Turn 3 — Reply 2 → broken promise → offer the split (contact 2)

**Client said** (inbound event 988):
> *"sorry, Friday didn't happen, can we do half now half next month"*

**Re-diagnosis:** `approver_bottleneck` → **`cash_stress`** (method llm/gemini) —
read the concrete "half now, half next month" as genuine cash difficulty and an
active renegotiation, **not** stalling/ghosting.

**Broken-promise handling + tier impact:** the Friday promise is recorded broken on
Brightline's trust record — `promises_broken` **0 → 1**. Tier recompute:
**amber → amber** (unchanged; `compute_tier` flips red only at `promises_broken ≥ 2`
or `worst_days_late ≥ 60` — Brightline's worst is 55). The invoice's
`promise_status` is cleared rather than set to `broken`: the client is
renegotiating in good faith, so the invoice keeps the cash_stress reading instead
of being forced to a stalling diagnosis.

**Decision:** `offer_payment_plan` · register **warm** · rule `table: cash_stress →
offer payment plan` · owner faiz. Guardrail: would block on `48h gap` → bypassed;
contacts 1 < cap 3 → passed.

**Real draft (`draft()`, warm):**
```
[Brightline Retail] Hi Brightline Retail team, samajhte hain quarter thoda tight hai.
Hum invoice INV-23 (Rs 300,000) ko instalments mein baant sakte hain - bata dijiye
kya suit karta hai. Pay here: https://rzp.io/rzp/vrnPlf3
```
**Delivered (stub):**
```
Hi, this is Support. We received your message and will assist shortly. Reply with
details. Test message from Twilio.
```
Sent · SID `MM5db907…2b36` · path `template` · contact #2.

---

## Turn 4 — Reply 3 → send the split (contact 3)

**Client said** (inbound event 991):
> *"yes please send the split"*

**Re-diagnosis (and an honest miss):** Gemini returned **`process_block`**, which
`decide` routes to `resubmit_via_finance` (Finance, **rhea**) — an *internal* action
with **no client message**. That's a misread: the client just *accepted* the
cash-stress split, so the right action is to send the plan. Logged the LLM output,
then applied a **manual correction back to `cash_stress`** (method
`manual_correction`, trigger `llm_misclassification`) so the invoice is handled as
the acceptance it is. This is the LLM path being fallible; the deterministic rules
and the audit trail both stay intact and the correction is recorded, not hidden.
The real fix is a diagnosis the classifier doesn't currently have: an **acceptance
intent** ("yes, send it" / "go ahead with the plan"). Today the seven labels have
no bucket for "client agreed to the proposed action," so an acceptance gets forced
into the nearest wrong label (here `process_block`). See the closing note.

**Decision (on corrected diagnosis):** `offer_payment_plan` · register **warm** ·
rule `table: cash_stress → offer payment plan` · owner faiz. Guardrail: would block
on `48h gap` → bypassed; contacts 2 < cap 3 → passed.

**Real draft (`draft()`, warm):**
```
[Brightline Retail] Hi Brightline Retail team, samajhte hain quarter thoda tight hai.
Hum invoice INV-23 (Rs 300,000) ko instalments mein baant sakte hain - bata dijiye
kya suit karta hai. Pay here: https://rzp.io/rzp/vrnPlf3
```
**Delivered (stub):**
```
Hi, this is Support. We received your message and will assist shortly. Reply with
details. Test message from Twilio.
```
Sent · SID `MMf6638e…23c6` · path `template` · **contact #3 — cap reached**.

_→ no reply; next scheduled action lets the contact cap fire (Turn 5)_

---

## Turn 5 — Contact cap reached → escalation

No reply. The agent's next scheduled action runs against state: diagnosis
`cash_stress`, **contacts_sent 3**, tier amber.

**Decision:** `escalate` · rule `override 4: contact cap (3) reached → escalate` ·
routed to **snehal** · routed_reason *"3 contacts sent (cap 3) — stop sending,
escalate."* Guardrail agrees the send path is closed (48h/​cap). No further client
message goes out.

**Escalation event written** (`stage=escalate`, `outcome=handover_queued`):
> INV-23 · Brightline Retail · ₹300,000 · cash_stress · 3 contacts + 1 broken
> promise · split accepted but unpaid → **snehal** (Head of Client Servicing).

The cap is the backstop: after three good-faith contacts the agent stops chasing
and hands a human the full context instead of sending a fourth message.

---

## Summary — what happened and why

| Turn | Client said | Agent concluded | Decision | Why |
|---|---|---|---|---|
| 1 | — (first contact) | cash_stress, amber | `offer_payment_plan` (warm) | table: cash_stress → plan |
| 2 | "delay… give us till Friday" | approver_bottleneck; **promise → 2026-09-11** | `do_nothing` (wait) | override 3: open promise → wait |
| 3 | "Friday didn't happen, half now half next month" | cash_stress; **promise broken** (pb 0→1, tier amber) | `offer_payment_plan` (warm) | renegotiation in good faith → offer the split |
| 4 | "yes please send the split" | *misread* `process_block` → **corrected to cash_stress** | `offer_payment_plan` (warm) | acceptance → send the plan (contact 3, cap) |
| 5 | — (no reply) | contacts 3 = cap | `escalate` → snehal | override 4: contact cap reached |

**Tier impact:** the broken Friday promise took Brightline `promises_broken` 0→1,
but tier held at **amber** — `compute_tier` flips red only at `promises_broken ≥ 2`
or `worst_days_late ≥ 60` (Brightline's worst is 55).

**Restraint:** the agent never sent while a promise was live (Turn 2), and stopped
at the cap rather than sending a fourth message (Turn 5).

### Honest note — the Turn 4 classifier miss
On "yes please send the split," Gemini returned **`process_block`** when the message
was an **acceptance of the payment plan**. Left alone, `decide` would have routed it
to internal Finance (`resubmit_via_finance`, rhea) with no client message — the
wrong handling for a client saying yes. It was **caught and manually corrected to
`cash_stress`**, and the correction is recorded in the event log (method
`manual_correction`, trigger `llm_misclassification`) rather than hidden. The proper
fix is a capability the classifier doesn't have yet: an **acceptance intent** ("the
client agreed to the proposed action"). None of the seven current diagnoses
(`not_actually_late`, `process_block`, `approver_bottleneck`, `wrong_contact`,
`dispute`, `cash_stress`, `stalling`) represent agreement, so an acceptance is
forced into the nearest — and wrong — label. Adding an `accepted`/`promise_to_pay`
intent (and a rule that sends the agreed action or confirms the plan) would close
this gap.

### Test conditions recap
Quiet hours and the 48h gap were bypassed for this test only; `policy.py` untouched.
Delivery used the approved template stub after `body=` was rejected with 21654; the
real `draft()` text is shown per turn for comparison. Sequence events are recorded
in `event_log`; they can be cleared to restore the canonical dataset.
