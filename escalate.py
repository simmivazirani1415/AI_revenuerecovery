"""Escalate stage: build the two human queues.

1. Escalations   - every invoice decide marked `escalate`, routed to the right
                   team member with a full handover pack.
2. Founder queue - every invoice flagged needs_founder_approval, shown with its
                   drafted message, register, amount, tier and history. Approve
                   or reject is a state change, not a send.

Both write `escalate` events carrying routed_to and routed_reason. Also renders
docs/escalations.md.
"""

import json
import os

import execute
from log import connect, write_event

HERE = os.path.dirname(os.path.abspath(__file__))
DOC_PATH = os.path.join(HERE, "docs", "escalations.md")


def _rupees(n):
    return f"Rs {n:,}"


def _contact_history(conn, invoice_id):
    rows = conn.execute(
        "SELECT timestamp, action_taken, outcome, message_sent, observed "
        "FROM event_log WHERE invoice_id = ? AND stage = 'execute' "
        "ORDER BY event_id", (invoice_id,)).fetchall()
    if not rows:
        return ["(no messages sent yet - agent has not contacted this client)"]
    hist = []
    for ts, act, outcome, msg, observed in rows:
        if act == "received_reply":
            reply = (json.loads(observed) or {}).get("reply_text", "") if observed else ""
            hist.append(f"{ts[:16]}  IN   \"{reply}\"")
        else:
            hist.append(f"{ts[:16]}  OUT  [{outcome}] {(msg or '')[:80]}")
    return hist


def _next_step(diagnosis, inv, client, routed_name):
    reply = (inv["reply_text"] or "").lower()
    broken = client["promises_broken"]
    if diagnosis == "dispute":
        return (f"Resolve the contested milestone/scope first. Hard stop on all "
                f"client chasing until {routed_name} signs it off.")
    if any(w in reply for w in ("stop chasing", "absurd", "ridiculous")):
        return ("Hostile reply - co-founder only, no de-escalation. Pause the "
                "account and consider a final notice.")
    if diagnosis == "cash_stress":
        return (f"Red tier with {broken} broken promise(s): weigh a structured "
                f"plan vs. recovery action. Do NOT auto-offer the payment plan.")
    if diagnosis == "stalling":
        return (f"{broken} promise(s) broken. Stop automated sends; move to a "
                f"direct human call / final notice. Consider holding future work.")
    return "Human review - decide next action."


def _load(conn):
    decide = {r["invoice_id"]: dict(r) for r in conn.execute(
        "SELECT invoice_id, decision, register, register_reason, routed_to, "
        "routed_reason, diagnosis, observed FROM event_log WHERE stage = 'decide'")}
    evidence = {r[0]: r[1] for r in conn.execute(
        "SELECT invoice_id, diagnosis_evidence FROM event_log WHERE stage = 'diagnose'")}
    invs = {r["invoice_id"]: dict(r) for r in conn.execute(
        """SELECT ai.*, c.name AS client_name, c.tier, c.segment,
                  c.relationship_years, c.total_revenue_inr, c.avg_days_to_pay,
                  c.worst_days_late, c.response_rate, c.promises_made,
                  c.promises_kept, c.promises_broken
           FROM agent_invoices ai JOIN clients c ON c.client_id = ai.client_id
           WHERE ai.status != 'paid'""")}
    team = {r[0]: r[1] for r in conn.execute("SELECT person_id, name FROM team")}
    return decide, evidence, invs, team


def run(conn):
    conn.execute("DELETE FROM event_log WHERE stage = 'escalate'")
    conn.commit()
    decide, evidence, invs, team = _load(conn)

    escalations, approvals = [], []
    for inv_id in sorted(decide):
        d, inv = decide[inv_id], invs.get(inv_id)
        if not inv:
            continue
        obs = json.loads(d["observed"] or "{}")

        # --- Queue 1: escalations ---
        if d["decision"] == "escalate":
            routed_name = team.get(d["routed_to"], d["routed_to"])
            pack = {
                "invoice_id": inv_id, "client": inv["client_name"],
                "tier": inv["tier"], "amount": inv["amount_inr"],
                "days_past_terms": inv["days_past_terms"],
                "diagnosis": d["diagnosis"], "evidence": evidence.get(inv_id, ""),
                "promises": f"{inv['promises_made']} made / "
                            f"{inv['promises_kept']} kept / {inv['promises_broken']} broken",
                "invoice_promise": inv["promise_status"],
                "history": _contact_history(conn, inv_id),
                "next_step": _next_step(d["diagnosis"], inv, inv, routed_name),
                "routed_to": d["routed_to"], "routed_name": routed_name,
                "routed_reason": d["routed_reason"],
            }
            escalations.append(pack)
            write_event(conn, invoice_id=inv_id, client_id=inv["client_id"],
                        stage="escalate", diagnosis=d["diagnosis"],
                        routed_to=d["routed_to"], routed_reason=d["routed_reason"],
                        action_taken="queued_escalation", outcome="pending_review",
                        observed={"queue": "escalation", "next_step": pack["next_step"]})

        # --- Queue 2: founder approval ---
        if obs.get("needs_founder_approval"):
            action = d["decision"]
            outstanding = inv["amount_inr"] - inv["amount_paid_inr"]
            msg = None
            if action in execute.CLIENT_FACING:
                msg = execute.draft(action, d["register"] or "neutral",
                                    inv["client_name"], inv_id, outstanding,
                                    inv["rzp_link_url"])
            item = {
                "invoice_id": inv_id, "client": inv["client_name"],
                "tier": inv["tier"], "segment": inv["segment"],
                "amount": inv["amount_inr"], "action": action,
                "register": d["register"], "register_reason": d["register_reason"],
                "message": msg,
                "history": (f"{inv['relationship_years']}y, "
                            f"{_rupees(inv['total_revenue_inr'])} lifetime, "
                            f"avg {inv['avg_days_to_pay']}d to pay, "
                            f"resp {inv['response_rate']}, promises "
                            f"{inv['promises_made']}/{inv['promises_kept']}/{inv['promises_broken']}"),
            }
            trigger = ("strategic" if inv["segment"] == "strategic"
                       else "above threshold")
            approvals.append(item)
            write_event(conn, invoice_id=inv_id, client_id=inv["client_id"],
                        stage="escalate", diagnosis=d["diagnosis"],
                        routed_to="aditya",
                        routed_reason=f"founder approval required ({trigger})",
                        action_taken="queued_for_approval", outcome="pending_approval",
                        observed={"queue": "approval", "action": action,
                                  "state": "pending_approval", "message": msg})
    return escalations, approvals, team


def render(escalations, approvals, team):
    lines = ["# Escalations & Founder Approval Queue", ""]

    # Queue 1 grouped by team member
    lines.append("## 1. Escalations (grouped by owner)")
    lines.append("")
    by_person = {}
    for e in escalations:
        by_person.setdefault((e["routed_to"], e["routed_name"]), []).append(e)
    for (pid, pname), items in sorted(by_person.items(), key=lambda x: x[0][1]):
        lines.append(f"### {pname}  ({len(items)})")
        for e in items:
            lines.append("")
            lines.append(f"**{e['invoice_id']} - {e['client']}** ({e['tier']} tier) "
                         f"- {_rupees(e['amount'])}, {e['days_past_terms']}d past terms")
            lines.append(f"- Diagnosis: **{e['diagnosis']}** - {e['evidence']}")
            lines.append(f"- Promises: {e['promises']}"
                         + (f"; invoice promise: {e['invoice_promise']}"
                            if e["invoice_promise"] else ""))
            lines.append(f"- Routed here because: {e['routed_reason']}")
            lines.append("- Contact history:")
            for h in e["history"]:
                lines.append(f"    - {h}")
            lines.append(f"- **Recommended next step:** {e['next_step']}")
        lines.append("")

    # Queue 2 approval queue
    lines.append(f"## 2. Founder Approval Queue ({len(approvals)})")
    lines.append("")
    lines.append("_Approve or reject is a state change, not a send. "
                 "State: `pending_approval`._")
    lines.append("")
    with_msg = [a for a in approvals if a["message"]]
    without = [a for a in approvals if not a["message"]]
    lines.append(f"### Drafted client sends awaiting sign-off ({len(with_msg)})")
    for a in with_msg:
        lines.append("")
        lines.append(f"**{a['invoice_id']} - {a['client']}** ({a['tier']} tier"
                     + (", strategic" if a["segment"] == "strategic" else "")
                     + f") - {_rupees(a['amount'])}")
        lines.append(f"- Action: {a['action']}  |  Register: **{a['register']}** "
                     f"- {a['register_reason']}")
        lines.append(f"- History: {a['history']}")
        lines.append(f"- Draft: {a['message']}")
    lines.append("")
    lines.append(f"### Above-threshold but no client send ({len(without)})")
    lines.append("_Flagged by the ₹3,00,000 / strategic rule, but the decision "
                 "sends nothing to the client - nothing to approve-to-send._")
    for a in without:
        lines.append(f"- **{a['invoice_id']}** {a['client']} - {_rupees(a['amount'])} "
                     f"- action `{a['action']}`")
    lines.append("")
    return "\n".join(lines)


def main():
    conn = connect()
    try:
        escalations, approvals, team = run(conn)
        doc = render(escalations, approvals, team)
    finally:
        conn.close()

    os.makedirs(os.path.dirname(DOC_PATH), exist_ok=True)
    with open(DOC_PATH, "w", encoding="utf-8") as f:
        f.write(doc)
    print(doc)
    print(f"\n(saved to {DOC_PATH})")


if __name__ == "__main__":
    main()
