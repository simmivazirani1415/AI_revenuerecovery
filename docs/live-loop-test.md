# Live WhatsApp loop — end-to-end test

A real inbound WhatsApp reply, received over Twilio → ngrok → Flask, re-diagnosed
and re-decided by the agent. This captures the full before/after of a single
invoice (INV-46, **Kestrel Events**) as the reply changed what the agent knew and
what it chose to do.

**Transport proven:** outbound template send (Twilio), inbound webhook
(`/webhook` via ngrok tunnel → local Flask on `:5001`), reply matched to the
most-recently contacted invoice, re-diagnosis, and re-decision — all live.

---

## Setup

- **Invoice:** INV-46 — Kestrel Events, ₹125,000, 20 days past terms, **red tier**.
- **Outbound:** sent as a WhatsApp template to the test phone. (The trial template
  `HX7cf5…` carries zero variable slots, so the outbound body is generic — this
  test exercises the **inbound** path, not the outbound copy.)
- **Inbound reply from WhatsApp:** *"cashflow is tight this month, can we split it"*

---

## ① Inbound reply event landed

The webhook received the reply, matched it to INV-46 (most-recent sent WhatsApp
event), and wrote an `execute` / `inbound_reply` event:

| Field | Value |
|---|---|
| stage / action | `execute` / `received_reply` |
| outcome | `inbound_reply` |
| invoice | INV-46 (client: Kestrel Events) |
| from | +91 81605 05326 |
| reply_text | `cashflow is tight this month, can we split it` |
| timestamp | 2026-09-04T20:16:20Z |

The reply text was also written back onto the invoice, which is what triggers the
re-diagnosis.

---

## ② Diagnosis flip

`diagnose_one` re-ran off the new reply text (`trigger=inbound_reply`):

| | Diagnosis | Basis |
|---|---|---|
| **Before** | `approver_bottleneck` | prior reply: "we're waiting on our own client to pay us first" → read as sign-off/approval pending |
| **After** | **`cash_stress`** | reply cites cash pressure ("cashflow is tight… can we split it") |

- **Method:** `deterministic` (cash-pressure keywords, no LLM needed)
- **Evidence:** *"Diagnosed cash_stress because the reply cites cash pressure
  ('cashflow is tight this month, can we split it'); INV-45 from the same client
  slipping too — offer the payment-plan template."*

---

## ③ Action change

Re-running `decide` on the new diagnosis changed the action:

| | Action | Register |
|---|---|---|
| **Before** | `ask_for_delegate` | neutral |
| **After** | **`escalate`** | firm |

- **Rule fired:** `table: cash_stress + red → escalate`
- **Register reason:** `red tier — firm register`
- **Routed to:** **aditya (co-founder)**
- **Routing reason:** `red-tier cash stress → escalate to co-founder, no plan offered`
- **Founder approval needed:** no (it's a co-founder handoff, not an approval hold)
- **Client message drafted:** none — `escalate` is not a client-facing action.

---

## The point: policy overrode the obvious next step

The diagnosis **evidence line itself suggested the payment-plan template** — the
natural, friendly response to "can we split it." A memoryless reminder tool would
have fired that plan offer straight back.

The agent didn't. Because it carries **memory of the client**, it knew two things
the reply alone doesn't say:

1. **Kestrel is red tier** — its payment history doesn't earn an unattended
   discount/plan offer.
2. **A sibling invoice (INV-45) from the same client is also slipping** — this is a
   broadening cash problem, not a one-off.

So the policy **overrode the payment-plan reflex** and escalated to a co-founder
(aditya) for a human call, in a firm register — *no automated plan offered*. Same
words from a green-tier client with a clean book would have earned a warm
payment-plan template; from red-tier Kestrel with a second invoice sliding, it
goes to a person. That override is the whole thesis: restraint and routing driven
by client memory, not by the last message received.

---

## Finding: voice runs in English (Hinglish tested, transcriber couldn't hold it)

The first voice calls were run in Hinglish to match how these conversations
actually happen. The Western (Vapi-default) transcriber **could not hold the
code-switching cleanly** — mid-call it bled Urdu script into the Hinglish
transcript and hedged gender on Hindi verb forms. That's a speech-to-text
limitation, not a pipeline one (the decide → brief → transcript → promise loop
worked either way).

Decision, recorded as a finding rather than hidden:

- **Voice now runs in professional Indian English.** `build_vapi_prompt` emits
  English tone descriptors and speaks amounts as English words ("three lakh
  rupees"), with the invoice number said separately from the amount.
- **The register system stays language-agnostic** — `decide.py` still picks
  warm / neutral / firm; only the spoken descriptor changed language.
- **Production would use an Indian-language speech model such as Sarvam** to handle
  Hindi/Hinglish code-switching natively. English-only is a transcriber constraint
  of this build, not a ceiling on the design.
