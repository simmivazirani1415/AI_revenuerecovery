"""Execute stage: draft, guardrail, and (optionally) send client messages.

For each decided invoice:
  - skip do_nothing / escalate (and resubmit_via_finance - that's internal)
  - hold anything needing founder approval (draft it, but don't send)
  - draft in the register decide chose: warm=Hinglish, neutral=plainer,
    firm=mostly English; append the Razorpay link for payment asks
  - prefix [CLIENT NAME] during development
  - every send passes the guardrail gate first
  - write an execute event (sent / held / blocked)

DRY RUN by default: prints drafts + register + guardrail result, sends nothing
and writes nothing. Pass --send to actually send via Twilio to MY_WHATSAPP and
persist events.
"""

import json
import sys
from datetime import datetime, timedelta

from log import connect, write_event
from policy import (CONTACT_CAP, IST, MIN_HOURS_BETWEEN_CONTACTS,
                    QUIET_HOURS_END, QUIET_HOURS_START)

CLIENT_FACING = {"nudge", "offer_payment_plan", "ask_for_ap_contact", "ask_for_delegate"}
PAYMENT_ACTIONS = {"nudge", "offer_payment_plan"}

HOSTILE_WORDS = ("stop chasing", "absurd", "ridiculous", "stop emailing")
BOUNCE_WORDS = ("bounce", "mailbox", "delivery failed", "undeliverable")
PAYMENT_WORDS = ("in transit", "payment initiated", "funds in transit",
                 "already paid", "payment sent")


# --------------------------------------------------------------------------
# Message drafting — tone follows the register decide picked.
# --------------------------------------------------------------------------
def draft(action, register, name, inv_id, amount_inr, link):
    amt = f"Rs {amount_inr:,}"
    T = {
        "nudge": {
            "warm": f"Hi {name} team! Chhota sa reminder - invoice {inv_id} ({amt}) "
                    f"abhi pending hai. Jab time mile, clear kar dijiye.",
            "neutral": f"Hello {name}, a quick reminder that invoice {inv_id} for "
                       f"{amt} is due.",
            "firm": f"{name}, invoice {inv_id} ({amt}) is overdue and needs to be "
                    f"settled at the earliest.",
        },
        "offer_payment_plan": {
            "warm": f"Hi {name} team, samajhte hain quarter thoda tight hai. Hum "
                    f"invoice {inv_id} ({amt}) ko instalments mein baant sakte hain - "
                    f"bata dijiye kya suit karta hai.",
            "neutral": f"Hello {name}, we understand cashflow is tight. We can split "
                       f"invoice {inv_id} ({amt}) into instalments - let us know what works.",
            "firm": f"{name}, on invoice {inv_id} ({amt}) we can offer an instalment "
                    f"plan, but we need a committed schedule.",
        },
        "ask_for_ap_contact": {
            "warm": f"Hi {name} team! Invoice {inv_id} sahi desk tak pahunche - aapke "
                    f"accounts payable ka contact mil sakta hai?",
            "neutral": f"Hello {name}, could you share your accounts-payable contact so "
                       f"invoice {inv_id} reaches the right desk?",
            "firm": f"{name}, invoice {inv_id} hasn't reached AP. Please share the "
                    f"correct accounts-payable contact.",
        },
        "ask_for_delegate": {
            "warm": f"Hi {name} team! Sign-off pending lag raha hai - koi delegate hai "
                    f"jo invoice {inv_id} ({amt}) approve kar sake?",
            "neutral": f"Hello {name}, sign-off on invoice {inv_id} ({amt}) seems "
                       f"pending. Is there a delegate who can approve it?",
            "firm": f"{name}, invoice {inv_id} ({amt}) is stuck awaiting approval. "
                    f"Please nominate a delegate to sign off.",
        },
    }
    body = T[action][register]
    if action in PAYMENT_ACTIONS and link:
        body += f" Pay here: {link}"
    return f"[{name}] {body}"


# --------------------------------------------------------------------------
# Guardrail gate — hard stops first, then timing gates.
# --------------------------------------------------------------------------
def guardrail(inv, diagnosis, contacts_sent, last_contact, now):
    reply = (inv["reply_text"] or "").lower()

    if diagnosis == "dispute":
        return False, "hard_stop:dispute"
    if inv["promise_status"] in ("kept", "pending"):
        return False, "hard_stop:open_promise"
    if inv["status"] == "paid" or any(w in reply for w in PAYMENT_WORDS):
        return False, "hard_stop:payment_received"
    if any(w in reply for w in HOSTILE_WORDS):
        return False, "hard_stop:hostile_reply"
    if inv["contact_verified"] == 0 and any(w in reply for w in BOUNCE_WORDS):
        return False, "hard_stop:dead_contact"

    if last_contact and (now - last_contact) < timedelta(hours=MIN_HOURS_BETWEEN_CONTACTS):
        return False, "rate_limit:48h_since_last_contact"
    cap = CONTACT_CAP["strategic" if inv["segment"] == "strategic" else "standard"]
    if contacts_sent >= cap:
        return False, f"contact_cap:{cap}_reached"
    if not (QUIET_HOURS_START <= now.hour < QUIET_HOURS_END):
        return False, "quiet_hours"
    return True, "passed"


def _twilio_send(body):
    import os
    from twilio.rest import Client
    client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"])
    msg = client.messages.create(
        from_=os.environ["TWILIO_WHATSAPP_FROM"],
        to=os.environ["MY_WHATSAPP"], body=body)
    return msg.sid


def run(conn, send=False):
    now = datetime.now(IST)
    decisions = {r["invoice_id"]: dict(r) for r in conn.execute(
        "SELECT invoice_id, decision, register, diagnosis, observed "
        "FROM event_log WHERE stage = 'decide'")}
    if not decisions:
        raise SystemExit("No decide events found - run decide.py first.")

    rows = {r["invoice_id"]: dict(r) for r in conn.execute(
        """SELECT ai.*, c.name AS client_name, c.tier, c.segment
           FROM agent_invoices ai JOIN clients c ON c.client_id = ai.client_id
           WHERE ai.status != 'paid'""")}

    results = []
    for inv_id in sorted(decisions):
        d = decisions[inv_id]
        inv = rows.get(inv_id)
        if not inv:
            continue
        action = d["decision"]
        register = d["register"] or "neutral"
        diagnosis = d["diagnosis"]
        needs_approval = json.loads(d["observed"]).get("needs_founder_approval", False)

        rec = {"invoice_id": inv_id, "client": inv["client_name"], "action": action,
               "register": register, "message": None, "status": None, "detail": None}

        # skip non-client-facing actions
        if action in ("do_nothing", "escalate"):
            rec["status"], rec["detail"] = "skip", action
            results.append(rec); continue
        if action == "resubmit_via_finance":
            rec["status"], rec["detail"] = "skip", "internal (finance), no client send"
            results.append(rec); continue

        outstanding = inv["amount_inr"] - inv["amount_paid_inr"]
        rec["message"] = draft(action, register, inv["client_name"], inv_id,
                               outstanding, inv["rzp_link_url"])

        # hold for founder approval
        if needs_approval:
            rec["status"], rec["detail"] = "held", "needs founder approval"
            results.append(rec)
            if send:
                write_event(conn, invoice_id=inv_id, client_id=inv["client_id"],
                            stage="execute", action_taken="held_for_approval",
                            message_sent=rec["message"], channel="none",
                            outcome="held_for_approval", register=register,
                            observed={"needs_founder_approval": True})
            continue

        # guardrail gate
        contacts = conn.execute(
            "SELECT COUNT(*), MAX(timestamp) FROM event_log WHERE invoice_id = ? "
            "AND stage = 'execute' AND outcome = 'sent'", (inv_id,)).fetchone()
        contacts_sent = contacts[0]
        last_contact = datetime.fromisoformat(contacts[1]) if contacts[1] else None

        allowed, reason = guardrail(inv, diagnosis, contacts_sent, last_contact, now)
        if not allowed:
            rec["status"], rec["detail"] = "blocked", reason
            if send:
                write_event(conn, invoice_id=inv_id, client_id=inv["client_id"],
                            stage="execute", action_taken="blocked",
                            message_sent=rec["message"], channel="none",
                            outcome=reason, register=register)
        else:
            rec["status"], rec["detail"] = ("sent" if send else "would_send"), reason
            if send:
                sid = _twilio_send(rec["message"])
                write_event(conn, invoice_id=inv_id, client_id=inv["client_id"],
                            stage="execute", action_taken="sent",
                            message_sent=rec["message"], channel="whatsapp",
                            outcome="sent", register=register,
                            observed={"twilio_sid": sid})
        results.append(rec)
    return results, now


def _report(results, now, send):
    mode = "SEND" if send else "DRY RUN (nothing sent, nothing written)"
    print(f"Execute - {mode}   IST now: {now.strftime('%Y-%m-%d %H:%M')} "
          f"(quiet hours {QUIET_HOURS_START}:00-{QUIET_HOURS_END}:00)\n")

    print(f"{'invoice':<8}{'client':<22}{'action':<20}{'reg':<8}{'status':<12}detail")
    for r in results:
        print(f"{r['invoice_id']:<8}{r['client'][:21]:<22}{r['action']:<20}"
              f"{r['register']:<8}{r['status']:<12}{r['detail']}")

    drafted = [r for r in results if r["message"]]
    print(f"\nDrafted messages ({len(drafted)}):")
    for r in drafted:
        flag = {"held": "HELD (founder approval)", "blocked": f"BLOCKED ({r['detail']})",
                "would_send": "WOULD SEND", "sent": "SENT"}[r["status"]]
        print(f"\n  {r['invoice_id']} [{r['register']}] -> {flag}")
        print(f"    {r['message']}")

    n_send = sum(1 for r in results if r["status"] in ("would_send", "sent"))
    n_held = sum(1 for r in results if r["status"] == "held")
    n_block = sum(1 for r in results if r["status"] == "blocked")
    n_skip = sum(1 for r in results if r["status"] == "skip")
    print(f"\nSummary: {n_send} sendable, {n_held} held, {n_block} blocked, "
          f"{n_skip} skipped  (of {len(results)})")


def main():
    send = "--send" in sys.argv
    conn = connect()
    try:
        results, now = run(conn, send=send)
        _report(results, now, send)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
