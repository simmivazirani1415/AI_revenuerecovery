# Escalations & Founder Approval Queue

## 1. Escalations (grouped by owner)

### Aditya Menon  (5)

**INV-33 - Trellis Infra** (red tier) - Rs 1,350,000, 71d past terms
- Diagnosis: **stalling** - Ground-truth diagnosis used (Gemini daily quota exhausted today; validated live at 91.7% earlier): stalling.
- Promises: 3 made / 0 kept / 3 broken; invoice promise: broken
- Routed here because: stalling -> escalate (co-founder, red handover)
- Contact history:
    - (no messages sent yet - agent has not contacted this client)
- **Recommended next step:** 3 promise(s) broken. Stop automated sends; move to a direct human call / final notice. Consider holding future work.

**INV-45 - Kestrel Events** (red tier) - Rs 140,000, 63d past terms
- Diagnosis: **stalling** - Ground-truth diagnosis used (Gemini daily quota exhausted today; validated live at 91.7% earlier): stalling.
- Promises: 2 made / 0 kept / 2 broken
- Routed here because: stalling -> escalate (co-founder, red handover); referred by Sable Properties -> escalate one step earlier (already at co-founder)
- Contact history:
    - (no messages sent yet - agent has not contacted this client)
- **Recommended next step:** 2 promise(s) broken. Stop automated sends; move to a direct human call / final notice. Consider holding future work.

**INV-58 - Saffron Media House** (amber tier) - Rs 150,000, 35d past terms
- Diagnosis: **stalling** - Ground-truth diagnosis used (Gemini daily quota exhausted today; validated live at 91.7% earlier): stalling.
- Promises: 1 made / 0 kept / 1 broken
- Routed here because: stalling -> escalate (snehal); referred by Vantage Consumer Group -> escalate one step earlier (to aditya)
- Contact history:
    - (no messages sent yet - agent has not contacted this client)
- **Recommended next step:** 1 promise(s) broken. Stop automated sends; move to a direct human call / final notice. Consider holding future work.

**INV-60 - Verma & Sons Developers** (red tier) - Rs 220,000, 79d past terms
- Diagnosis: **cash_stress** - Ground-truth diagnosis used (Gemini daily quota exhausted today; validated live at 91.7% earlier): cash_stress.
- Promises: 2 made / 0 kept / 2 broken
- Routed here because: red-tier cash stress -> escalate to co-founder, no plan offered
- Contact history:
    - (no messages sent yet - agent has not contacted this client)
- **Recommended next step:** Red tier with 2 broken promise(s): weigh a structured plan vs. recovery action. Do NOT auto-offer the payment plan.

**INV-61 - Verma & Sons Developers** (red tier) - Rs 210,000, 25d past terms
- Diagnosis: **stalling** - Ground-truth diagnosis used (Gemini daily quota exhausted today; validated live at 91.7% earlier): stalling.
- Promises: 2 made / 0 kept / 2 broken
- Routed here because: hostile reply -> route to co-founder, no de-escalation
- Contact history:
    - (no messages sent yet - agent has not contacted this client)
- **Recommended next step:** Hostile reply - co-founder only, no de-escalation. Pause the account and consider a final notice.

### Karan Bhatt  (1)

**INV-34 - Trellis Infra** (red tier) - Rs 1,200,000, 35d past terms
- Diagnosis: **dispute** - Ground-truth diagnosis used (Gemini daily quota exhausted today; validated live at 91.7% earlier): dispute.
- Promises: 3 made / 0 kept / 3 broken
- Routed here because: dispute -> project lead for the service line (karan), copy Snehal
- Contact history:
    - (no messages sent yet - agent has not contacted this client)
- **Recommended next step:** Resolve the contested milestone/scope first. Hard stop on all client chasing until Karan Bhatt signs it off.

### Snehal Rao  (1)

**INV-19 - Halcyon Pharma** (green tier) - Rs 1,250,000, -15d past terms
- Diagnosis: **dispute** - Ground-truth diagnosis used (Gemini daily quota exhausted today; validated live at 91.7% earlier): dispute.
- Promises: 0 made / 0 kept / 0 broken
- Routed here because: dispute -> project lead for the service line (karan), copy Snehal; referred by Sable Properties -> escalate one step earlier (to snehal)
- Contact history:
    - (no messages sent yet - agent has not contacted this client)
- **Recommended next step:** Resolve the contested milestone/scope first. Hard stop on all client chasing until Snehal Rao signs it off.

## 2. Founder Approval Queue (12)

_Approve or reject is a state change, not a send. State: `pending_approval`._

### Drafted client sends awaiting sign-off (4)

**INV-11 - Sable Properties** (green tier, strategic) - Rs 320,000
- Action: nudge  |  Register: **warm** - strategic account - protect the relationship; referrer of 3 clients -> one notch warmer, +7d patience before first contact
- History: 4.0y, Rs 2,200,000 lifetime, avg 50.0d to pay, resp 0.88, promises 1/1/0
- Draft: [Sable Properties] Hi Sable Properties team! Chhota sa reminder - invoice INV-11 (Rs 320,000) abhi pending hai. Jab time mile, clear kar dijiye. Pay here: https://rzp.io/rzp/o0AGL91J

**INV-28 - Corvid Sports Network** (amber tier) - Rs 1,600,000
- Action: ask_for_ap_contact  |  Register: **neutral** - amber tier, routine handling
- History: 2.0y, Rs 4,200,000 lifetime, avg 51.7d to pay, resp 0.6, promises 1/0/1
- Draft: [Corvid Sports Network] Hello Corvid Sports Network, could you share your accounts-payable contact so invoice INV-28 reaches the right desk?

**INV-29 - Corvid Sports Network** (amber tier) - Rs 1,800,000
- Action: nudge  |  Register: **neutral** - amber tier, routine handling
- History: 2.0y, Rs 4,200,000 lifetime, avg 51.7d to pay, resp 0.6, promises 1/0/1
- Draft: [Corvid Sports Network] Hello Corvid Sports Network, a quick reminder that invoice INV-29 for Rs 600,000 is due.

**INV-55 - Vantage Consumer Group** (green tier, strategic) - Rs 240,000
- Action: nudge  |  Register: **warm** - strategic account - protect the relationship
- History: 5.0y, Rs 14,400,000 lifetime, avg 12.2d to pay, resp 0.95, promises 0/0/0
- Draft: [Vantage Consumer Group] Hi Vantage Consumer Group team! Chhota sa reminder - invoice INV-55 (Rs 240,000) abhi pending hai. Jab time mile, clear kar dijiye. Pay here: https://rzp.io/rzp/4XSZ3NQ

### Above-threshold but no client send (8)
_Flagged by the ₹3,00,000 / strategic rule, but the decision sends nothing to the client - nothing to approve-to-send._
- **INV-04** Aureus Motors - Rs 480,000 - action `do_nothing`
- **INV-05** Aureus Motors - Rs 900,000 - action `resubmit_via_finance`
- **INV-14** Nexora Telecom - Rs 720,000 - action `do_nothing`
- **INV-15** Nexora Telecom - Rs 460,000 - action `resubmit_via_finance`
- **INV-19** Halcyon Pharma - Rs 1,250,000 - action `escalate`
- **INV-33** Trellis Infra - Rs 1,350,000 - action `escalate`
- **INV-34** Trellis Infra - Rs 1,200,000 - action `escalate`
- **INV-56** Vantage Consumer Group - Rs 240,000 - action `do_nothing`
