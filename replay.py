"""Replay one invoice's full event trail in readable plain text.

    ./venv/bin/python replay.py INV-33

Walks the event_log in order and renders each stage - what was observed, the
diagnosis and its evidence, the decision and rule, the register and why, the
routing and why - as prose. No JSON dumps.
"""

import sys

from log import connect, get_history


def rs(n):
    return f"Rs {int(n or 0):,}"


def _detect(e, team):
    o = e["observed"] or {}
    verdict = (f"flagged AT RISK (risk score {o.get('risk_score')})"
               if o.get("at_risk") else "left alone")
    extra = []
    if o.get("promise_status"):
        extra.append(f"promise {o['promise_status']}")
    if o.get("po_matched") == 0:
        extra.append("PO mismatch")
    if o.get("contact_verified") == 0:
        extra.append("contact unverified")
    if o.get("contacts_sent"):
        extra.append(f"{o['contacts_sent']} prior contacts")
    seen = (f"{rs(o.get('outstanding_inr'))} outstanding, "
            f"{o.get('days_past_terms')}d past net-{o.get('terms_days')} terms, "
            f"{o.get('tier')} tier"
            + (", " + ", ".join(extra) if extra else ""))
    return [f"Observed : {seen}",
            f"Verdict  : {verdict}",
            f"Why      : {o.get('reason')}"]


def _diagnose(e, team):
    o = e["observed"] or {}
    method = o.get("method")
    tail = f"  (via {method})" if method else ""
    return [f"Diagnosis: {e['diagnosis']}{tail}",
            f"Evidence : {e['diagnosis_evidence']}"]


def _decide(e, team):
    o = e["observed"] or {}
    lines = [f"Decision : {e['decision']}",
             f"Rule     : {e['rule_applied']}"]
    if e["register"]:
        lines.append(f"Register : {e['register']} - {e['register_reason']}")
    if e["routed_to"]:
        lines.append(f"Routing  : {team.get(e['routed_to'], e['routed_to'])} "
                     f"- {e['routed_reason']}")
    if o.get("needs_founder_approval"):
        lines.append("Approval : founder sign-off required before any send")
    return lines


def _escalate(e, team):
    o = e["observed"] or {}
    who = team.get(e["routed_to"], e["routed_to"])
    if o.get("queue") == "approval":
        lines = [f"Queue    : founder approval -> {who}",
                 f"Reason   : {e['routed_reason']}",
                 f"State    : {o.get('state', 'pending_approval')} "
                 "(approve/reject is a state change, not a send)"]
        if o.get("message"):
            lines.append(f"Drafted  : {o['message']}")
        else:
            lines.append("Drafted  : (no client message - non-sending action)")
        return lines
    return [f"Queue    : escalation -> {who}",
            f"Reason   : {e['routed_reason']}",
            f"Next step: {o.get('next_step', '(see handover pack)')}"]


def _execute(e, team):
    o = e["observed"] or {}
    if e["action_taken"] == "received_reply":
        return [f"Inbound  : \"{o.get('reply_text', '')}\" (from {o.get('from', '?')})"]
    lines = [f"Action   : {e['action_taken']}  [{e['outcome']}]"]
    if e["channel"] and e["channel"] not in ("none", "internal"):
        lines.append(f"Channel  : {e['channel']}")
    if e["message_sent"]:
        lines.append(f"Message  : {e['message_sent']}")
    return lines


RENDER = {"detect": _detect, "diagnose": _diagnose, "decide": _decide,
          "escalate": _escalate, "execute": _execute}


def replay(conn, invoice_id):
    hdr = conn.execute(
        """SELECT i.amount_inr, i.amount_paid_inr, i.terms_days, i.days_past_terms,
                  i.status, c.name, c.tier, c.segment
           FROM invoices i JOIN clients c ON c.client_id = i.client_id
           WHERE i.invoice_id = ?""", (invoice_id,)).fetchone()
    if not hdr:
        print(f"No such invoice: {invoice_id}")
        return
    team = {r[0]: r[1] for r in conn.execute("SELECT person_id, name FROM team")}
    events = get_history(conn, invoice_id)

    out = hdr["amount_inr"] - hdr["amount_paid_inr"]
    amt = (rs(hdr["amount_inr"]) if out == hdr["amount_inr"]
           else f"{rs(out)} balance of {rs(hdr['amount_inr'])}")
    print(f"\n=== Replay: {invoice_id} - {hdr['name']} "
          f"({hdr['tier']} tier{', strategic' if hdr['segment']=='strategic' else ''}) ===")
    print(f"{amt}, net-{hdr['terms_days']}, {hdr['days_past_terms']}d past terms, "
          f"status {hdr['status']}")

    if not events:
        print("\n(no events logged yet)")
        return
    for i, e in enumerate(events, 1):
        stamp = (e["timestamp"] or "")[:16].replace("T", " ")
        print(f"\n[{i}] {e['stage'].upper():<9} {stamp}")
        for line in RENDER.get(e["stage"], lambda *_: [])(e, team):
            print(f"    {line}")
    print()


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: replay.py <INVOICE_ID> [INVOICE_ID ...]")
    conn = connect()
    try:
        for inv_id in sys.argv[1:]:
            replay(conn, inv_id)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
