# Receivables AI

An autonomous B2B receivables agent that diagnoses *why* each invoice is unpaid and takes a proportionate action nudge, wait, route internally, call, or escalate instead of blindly chasing.

## The problem

An agency had **₹3.4L stuck across six invoices**, the oldest **67 days past terms** while those same clients were still replying normally on work chats every day. They'd read the reminders and simply not paid, so sending more was never the fix. And of the seven causes we identified, **four are the agency's own paperwork problem** (PO mismatch, wrong AP contact, a sign-off sitting with someone on leave, an invoice chased while still inside terms), not the client's unwillingness. A cadence-based reminder tool can't tell those apart it just re-sends.

## What already exists, and the gap

Razorpay's **Agent Studio** launched in **March 2026** on the **Claude Agent SDK** with eight agents, including voice cart recovery and subscription recovery. Every one of them is scoped to a **single event** a checkout, a failed charge — and forgets the counterparty the moment it's done. **That forgetting is the gap.** Recovery isn't an event; it's a relationship over time: this is the client's third broken promise, that one is a strategic account you never chase firmly, this client was referred by your best account so soften the tone. None of that survives an event-scoped agent. Receivables AI keeps it, and every decision is grounded in it.

## How it works

Five stages run over a strict view of the ledger, each writing a full audit row to `event_log`:

| Stage | What it does |
|---|---|
| **detect** | Triage each open invoice on days *past terms* (not days since issue). Leave inside-terms invoices alone unless a structural blocker is present. |
| **diagnose** | Classify into one of seven causes. Deterministic structural checks first → Gemini on the reply text only if inconclusive → tier-based default. |
| **decide** | Pure rules. An ordered decision table picks the action, the register (warm/neutral/firm) and the routing, and applies referral + founder-approval policy. |
| **execute** | Draft in the chosen register, pass the guardrail gate, send (dry-run by default). |
| **escalate** | Hand off to the right human with a full pack, or hold for founder approval. Voice is rung 4, before the human handover. |

Underneath sit six layers: the **ledger** (`agency.db`, read through an `agent_invoices` view that hides ground truth), **memory** (client tier, promises, payment history, referral ties), **policy** (`policy.py` — caps, quiet hours, thresholds), **decision** (`decide.py`), **channel** (WhatsApp, voice, email), and **interface** (the Flask dashboard). The split that matters: **rules decide *whether* to act; the client profile decides *how*.** The same "we're tight on cash" reply earns a warm payment-plan offer from a green-tier client and a firm co-founder escalation from a red-tier one with a second invoice already slipping.

## Results

Measured against a hand-labelled ground truth of 24 open invoices across 15 clients.

- **Diagnosis: 91.7% (22/24)** on Gemini (`gemini-3.6-flash`), identical across two consecutive runs (answers cached, so reproducible), **no fallback fired**. Method is tiered: deterministic structural checks resolve the clear cases, the LLM reads the reply text only when they're inconclusive, and a tier default is the last resort.
- **Decide: 24/24** against `gt_correct_action` when fed the correct diagnoses — the decision rules themselves are complete.
- **0 invoices wrongly left alone** in the simulated run restraint that's correct, not restraint that misses a real problem.
- **Counterfactual 188 vs 3.** A memoryless reminder tool chasing everything past due on a 7-day cadence fires **188 messages**; this agent auto-sends **3**. Of the naive tool's messages, 8 land on clients inside terms, 2 on active disputes, 3 firm-toned at strategic accounts, 1 into a dead mailbox. The agent: **zero in every harm category.**
- **Net-30 sensitivity (the strongest point).** Even a *smarter* naive tool that knew the correct payment terms drops to **92 messages** and stops dunning inside-terms invoices — but it **still** chases 1 dispute, stays firm on 1 strategic account, and messages 1 dead contact. Better date maths removes the terms-blindness harm; the memory-blindness harm remains. That residue is exactly what a per-client profile fixes and a cadence never will.

Full breakdown in [`docs/founder_report.md`](docs/founder_report.md).

## Guardrails

Every client-facing send passes a gate first, checked against a simulated clock:

- **Contact caps** — 3 for standard clients, 2 for strategic; hitting the cap escalates instead of sending again.
- **Quiet hours** — 09:00–19:00 IST only. This gate **blocked live sends during testing and had to be explicitly bypassed** to run the voice and WhatsApp tests — the guardrail is real, not decorative.
- **48-hour gap** between contacts on the same invoice.
- **Five hard stops** — never send on a `dispute`, an open (kept/pending) `promise`, `payment_received`, a `hostile_reply`, or a `dead_contact`.

Plus a founder-approval hold above **₹3,00,000** or for strategic clients, and a 7-day referral-patience delay before the first contact to a client who has referred others.

## The live conversation

A five-turn WhatsApp negotiation on one invoice (INV-23, Brightline), played out over the live webhook: first contact → *"give us till Friday"* (promise captured, agent **waits**) → *"Friday didn't happen, half now half next month"* (promise broken, `promises_broken` 0→1, tier held at amber) → *"yes please send the split"* → cap reached → **escalation to Snehal**. Full turn-by-turn in [`docs/live-sequence-test.md`](docs/live-sequence-test.md).

Recorded honestly: on Turn 4, Gemini read *"yes please send the split"* as `process_block` when it was an **acceptance** of the plan. It was caught and manually corrected the real fix is a capability the **seven-label set doesn't have: an acceptance intent**. Agreement gets forced into the nearest wrong label.

## Voice

Voice is **rung 4** of the escalation ladder one automated call after the text contacts are spent, before the human handover, only for consenting clients. It's a browser web call (Vapi Web SDK; no phone number, no telephony cost), the brief is composed from the live client profile and the register `decide` chose, and the transcript is ingested back into the promise register automatically. Every call gets a computed founder review at `/calls` (commitment y/n, confirmation count, talk ratio, rule adherence).

Hinglish was tested first, to match how these calls really happen. The Western (Vapi-default) transcriber **couldn't hold the code-switching** — Urdu-script bleed and gender hedging on Hindi verb forms. So voice **runs in English**, the register system stays language-agnostic, and **production would use an Indian-language speech model like Sarvam**. Recorded as a finding, not hidden — see [`docs/voice-rung.md`](docs/voice-rung.md).

## What's built vs planned

| Area | Built | Planned |
|---|---|---|
| Five-stage pipeline over the ledger | ✅ | |
| Dashboard: clients, invoices, activity, invoice trail, calls review, configure | ✅ | |
| WhatsApp send + inbound webhook + re-diagnosis | ✅ | |
| Voice call (web), transcript → promise, call review | ✅ | |
| Post-call email summaries (Resend) | ✅ | |
| Draft-a-message (real `draft()`) and Share-as-PDF actions | ✅ | |
| AI copilot — chip-driven answers + real drafts | ✅ | |
| Free-text chat in the copilot | | Planned |
| Send actions from the UI (email / follow-up) | | Planned |
| Document parsing (read uploaded contracts for terms) | | Planned |
| Negotiator (multi-round settlement logic) | | Planned |

## Known limitations

- **Twilio trial blocks custom templates**, so delivery is proven on Twilio's stub template with real message SIDs the send path (auth, From/To, content send) works; the stub just carries no invoice data.
- **Resend's shared sender** (`onboarding@resend.dev`) only delivers to the account owner, so every demo email is overridden to one inbox; team `example.com` addresses appear as "Would route to", never as real recipients.
- **The dataset is synthetic and authored** — which is precisely what makes the accuracy figures measurable, since every invoice carries a hand-labelled ground truth to score against.
- **google-genai transport bug**: on `google-genai` 1.47.0 a 429 wedges the client's transport for the rest of the process (it surfaces as "client has been closed"). The code recognises it and treats it as a rate-limit; successful classifications are cached so a warmed run is stable.

## Running it

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

./venv/bin/python generate_data.py         # build agency.db (8 team, 15 clients, 61 invoices)
./venv/bin/python create_payment_links.py  # Razorpay test-mode links for actionable invoices
./venv/bin/python run_all.py               # simulated-clock end-to-end run
./venv/bin/python ui.py                     # dashboard on http://localhost:5002
```

Copy `.env.example` to `.env` and fill in what you need — the UI and pipeline run against the generated ledger without any keys:

- **Razorpay** — `RZP_KEY_ID`, `RZP_KEY_SECRET` (payment links)
- **Twilio** — `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_WHATSAPP_FROM`, `MY_WHATSAPP`, `CONTENT_SID` (WhatsApp)
- **LLM** — `GEMINI_API_KEY` (primary), `OPENAI_API_KEY` (fallback)
- **Vapi** — `VAPI_PUBLIC_KEY`, `VAPI_API_KEY`, `VAPI_ASSISTANT_ID` (voice)
- **Resend** — `RESEND_API_KEY`, `FROM_EMAIL`, `DEMO_EMAIL` (email summaries)

## Repo map

| Module | Purpose |
|---|---|
| `generate_data.py` | Build the synthetic ledger; tier computed from history, not hardcoded |
| `verify_data.py` | Sanity-check coverage, edge cases, restraint |
| `detect.py` | Stage 1 — triage open invoices on days past terms |
| `diagnose.py` | Stage 2 — classify the cause (deterministic → LLM → tier) |
| `decide.py` | Stage 3 — the decision table, registers, routing, overrides |
| `execute.py` | Stage 4 — draft, guardrail gate, send |
| `escalate.py` | Stage 5 — human handover packs + founder approval queue |
| `policy.py` | Caps, quiet hours, thresholds, referral policy |
| `llm.py` | Gemini/OpenAI provider chain + file cache |
| `log.py` | `event_log` writer/reader — one row reconstructs a whole decision |
| `voice_bridge.py` | Vapi brief builder + transcript ingest (rung 4) |
| `call_review.py` | Call list, detail, computed review + invoice-trail PDF |
| `email_summary.py` | Post-call Resend summaries |
| `founder_report.py` | Six-section report + the counterfactual |
| `run_all.py` | Simulated-clock end-to-end pipeline |
| `replay.py` | Plain-text event trail for one invoice |
| `create_payment_links.py` | Razorpay test-mode payment links |
| `inbound.py` | Flask webhooks — Twilio inbound + Vapi end-of-call |
| `ui.py` | The dashboard (Flask, port 5002) |

> Simulated ledger, Razorpay test mode. All client names are fictional.
