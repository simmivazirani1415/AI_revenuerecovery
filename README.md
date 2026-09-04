# Receivables AI

An autonomous B2B receivables-recovery agent that diagnoses *why* each invoice is
unpaid and takes a proportionate action — nudge, wait, route internally, or
escalate to the right person — instead of blindly chasing. It carries memory of
the client (tier, promises, payment history, referral ties) across every
interaction, so restraint is a first-class outcome: knowing when **not** to send
is as important as sending.

---

## The problem

An agency had **₹3.4L stuck across six invoices**, the oldest **67 days** overdue —
while those same clients were still replying normally on work chats every day.
The invoices weren't unpaid because the clients had vanished; they were unpaid
because of PO mismatches, sign-offs pending someone's return, quarter-end
freezes, and a couple of genuine disputes. **More messages was never the fix.**
A reminder tool that just re-sends on a cadence makes the good relationships
worse and never touches the actual blocker.

---

## Where this fits: Razorpay Agent Studio

Razorpay's Agent Studio already ships **eight agents on the Claude Agent SDK** —
including the voice shopping cart and subscription-recovery agents. They're
excellent, and they're all **event-scoped**: each one handles a moment (a
checkout, a failed charge) and then forgets the counterparty.

The gap this fills is **memory of the client**. Recovery isn't an event, it's a
relationship over time: this invoice is the client's third broken promise, that
one is a strategic account you should never chase firmly, this client was
referred by your best account so soften the tone. None of that survives an
event-scoped agent. Receivables AI keeps it, and every decision is grounded in it.

---

## The pipeline

Five stages, each reading a strict view of the ledger and writing a full audit
trail to `event_log`. A simulated clock (`run_all.py`) advances across the
timeline so sequences actually play out — first contact, 48-hour gap, second
contact, cap reached, escalate — with every guardrail checked against sim time.

| Stage | What it does |
|---|---|
| **detect** | Triage each open invoice on **days past terms** (not days since issue), amount, tier, prior contacts, promises. Inside-terms invoices are left alone unless a structural blocker (PO mismatch, failed auto-debit, dead contact) is present. |
| **diagnose** | Classify into one of seven diagnoses. Deterministic structural checks first → LLM on the reply text only if inconclusive → tier-based default. |
| **decide** | Pure rules, no LLM. A decision table with ordered overrides picks the action, register (warm / neutral / firm) and routing, and applies referral + founder-approval policy. |
| **execute** | Draft in the chosen register, pass the guardrail gate, send (dry-run by default). |
| **escalate** | Route to the right human with a full handover pack, or hold for founder approval. |

The seven diagnoses: `not_actually_late`, `process_block`, `approver_bottleneck`,
`wrong_contact`, `dispute`, `cash_stress`, `stalling`.

---

## Results

Measured against a hand-labelled ground truth of 24 open invoices across 15 clients.

- **Diagnosis: 91.7% (22/24)** on Gemini (`gemini-3.6-flash`), identical across
  **two consecutive runs** (answers cached, so it's reproducible), with **no
  fallback fired**. The two misses are honest: a vague "we'll pay by the 25th"
  read as stalling, and "funds in transit" read as payment-incoming.
- **Decide: 24/24** against `gt_correct_action` when fed the correct diagnoses —
  the decision rules themselves are complete.
- **0 invoices wrongly left alone** in the simulated run — restraint that's
  correct, not restraint that misses a real problem.
- **Counterfactual — 188 vs 4.** A memoryless reminder tool chasing everything
  past due on a 7-day cycle would fire **188 messages**; this agent sends **4**.
  Of the naive tool's messages: 8 land on clients still inside terms, 2 on active
  disputes, 3 firm-toned at strategic accounts, 1 into a dead mailbox. The agent:
  zero in every harm category.
- **Net-30 sensitivity.** Even a *smarter* naive tool that knew the correct
  payment terms would drop to **92 messages** and stop dunning inside-terms
  invoices — but it would **still** chase 1 dispute, stay firm on 1 strategic
  account, and message 1 dead contact. Those need memory of the client, not
  better date maths.

---

## Guardrails

Every client-facing send passes a gate first, checked against the (simulated) clock:

- **Quiet hours** — only 9:00–19:00 IST.
- **48-hour gap** between contacts on the same invoice.
- **Tier-based contact caps** — 3 for standard clients, 2 for strategic; hitting
  the cap escalates instead of sending again.
- **Five hard stops** — never send on a `dispute`, an open (kept/pending)
  `promise`, `payment_received`, a `hostile_reply`, or a `dead_contact`.

Plus a founder-approval hold above ₹3,00,000 or for strategic clients, and a
7-day referral-patience delay before the first contact to a client who has
referred others.

---

## Built vs planned (honestly)

**Built and real**
- The full five-stage pipeline over `agency.db`, every figure traceable to a query.
- The web UI (`ui.py`, Flask, port 5002): client list, invoice detail with the
  decision trail, agent-activity dashboard, onboarding, and a Configure page.
- The AI copilot drawer — chip-driven questions answered from `event_log` and the
  invoices, and **real reminder drafts** produced by the actual `execute.draft()`
  using the register `decide` chose.
- Live feedback capture, onboarding settings, uploads and team edits — all persisted.

**Planned (and labelled as such in the UI)**
- Free-text chat in the copilot (currently echoes the question and says it isn't wired).
- Send actions from the UI ("Send via email", "Set a follow-up").
- Document parsing — uploaded contracts are stored but not yet read for terms.
- Voice.

---

## Twilio (WhatsApp) status

The send path is **proven in code**: `execute._twilio_send` supports both an
approved-template path (`content_sid` + `content_variables`) and a raw-body
fallback, normalizes the `whatsapp:` prefix, and logs which path was used.
**Live sends are currently blocked by an account-level template requirement** —
Twilio returns `21654 "The ContentSid is Invalid"` for the supplied template, so
no message has actually gone out. Every rejection is captured **per invoice** as
a `send_failed` event with the exact error; the batch never crashes on a bad send.

## Known issue: google-genai transport

On `google-genai` 1.47.0 (Python 3.9), a **429 rate-limit wedges the client's
transport for the rest of the process** — it surfaces as "client has been closed"
rather than a rate-limit error, so one throttled call can silently break every
subsequent LLM call in the same run. The code recognises this and treats it as a
rate-limit; successful classifications are cached to `llm_cache.json` so a warmed
run is stable and reproducible.

---

## Run it

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

./venv/bin/python generate_data.py     # builds agency.db (team, 15 clients, 61 invoices)
./venv/bin/python verify_data.py        # sanity-checks coverage and restraint

./venv/bin/python ui.py                 # dashboard on http://localhost:5002
```

Copy `.env.example` to `.env` and fill in keys (Razorpay, Twilio, Gemini/OpenAI)
to exercise the payment-link and LLM paths; the UI and pipeline run without them
against the generated ledger.

Other entry points: `run_all.py` (simulated-clock end-to-end run),
`replay.py <INVOICE_ID>` (plain-text decision trail for one invoice),
`founder_report.py` (six-section report + the counterfactual).

> Simulated ledger, Razorpay test mode. All client names are fictional.
