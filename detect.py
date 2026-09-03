"""Detect stage: triage every open invoice into at-risk vs leave-alone.

Reads ONLY the agent_invoices view (never the invoices table) plus client
profiles. Risk is judged on days PAST TERMS, not days since issue, so an
invoice comfortably inside net-60 is not late. Signals considered:
  - payment terms / days past terms   (lateness relative to the client's terms)
  - amount at stake                    (outstanding balance)
  - client tier                        (green / amber / red)
  - contacts already sent              (from the event_log)
  - open / broken promise              (broken promise raises risk)

Writes one 'detect' event per invoice considered, at-risk or not, with the
reasoning captured in the observed snapshot. Returns a ranked at-risk list.
"""

import os
import sqlite3

from log import connect, write_event

TIER_WEIGHT = {"green": 1, "amber": 2, "red": 3}


def _contacts_sent(conn, invoice_id):
    """How many client-facing messages we've already sent for this invoice."""
    row = conn.execute(
        "SELECT COUNT(*) FROM event_log "
        "WHERE invoice_id = ? AND stage = 'execute' "
        "AND channel IS NOT NULL AND channel NOT IN ('none', 'internal')",
        (invoice_id,),
    ).fetchone()
    return row[0]


def assess(inv, tier, contacts_sent):
    """Return (at_risk, risk_score, reason) for one invoice row (dict)."""
    dpt = inv["days_past_terms"]
    terms = inv["terms_days"]
    outstanding = inv["amount_inr"] - inv["amount_paid_inr"]
    lakhs = outstanding / 100000
    within_terms = dpt is not None and dpt < 0

    blocker = (
        inv["po_matched"] == 0
        or inv["contact_verified"] == 0
        or inv["status"] == "partially_paid"
    )
    broken_promise = inv["promise_status"] == "broken"
    has_reply = bool(inv["reply_text"])

    # Restraint: inside terms, no blocker, and the client is silent -> not late.
    # A reply means the client is engaging, so forward it for diagnose to read.
    if within_terms and not blocker and not has_reply:
        reason = (f"Inside net-{terms} terms ({-dpt}d before due), no blocker, "
                  f"client silent - not late, leave alone.")
        return False, 0.0, reason

    # Risk score. Lateness uses days PAST TERMS (floored at 0), so being early
    # never adds risk; a known blocker still carries the amount at stake.
    lateness = max(0, dpt or 0)
    risk = lakhs * TIER_WEIGHT.get(tier, 2) * (1 + lateness / 30)
    if broken_promise:
        risk *= 1.5
    if blocker:
        risk *= 1.15
    risk *= 1 + 0.10 * contacts_sent
    risk = round(risk, 1)

    if within_terms and blocker:
        reason = (f"Inside net-{terms} terms but a hard blocker is present "
                  f"(po_matched={inv['po_matched']}, "
                  f"contact_verified={inv['contact_verified']}, "
                  f"status={inv['status']}) on Rs {outstanding:,} - at risk.")
    elif within_terms:
        reason = (f"Inside net-{terms} terms ({-dpt}d before due) but the client "
                  f"has replied - forward to diagnose to read the message.")
    else:
        bits = [f"{dpt}d past net-{terms} terms", f"Rs {outstanding:,} at stake",
                f"{tier} tier"]
        if broken_promise:
            bits.append("broken promise")
        if contacts_sent:
            bits.append(f"{contacts_sent} contacts already sent")
        reason = ", ".join(bits) + " - at risk."
    return True, risk, reason


def run(conn, write_events=True):
    """Assess all open invoices. Returns dict with ranked 'at_risk' + 'left_alone'."""
    if write_events:
        conn.execute("DELETE FROM event_log WHERE stage = 'detect'")
        conn.commit()

    rows = conn.execute(
        """
        SELECT ai.*, c.tier AS tier, c.name AS client_name
        FROM agent_invoices ai
        JOIN clients c ON c.client_id = ai.client_id
        WHERE ai.status != 'paid'
        ORDER BY ai.invoice_id
        """
    ).fetchall()

    at_risk, left_alone = [], []
    for r in rows:
        inv = dict(r)
        contacts = _contacts_sent(conn, inv["invoice_id"])
        flagged, risk, reason = assess(inv, inv["tier"], contacts)

        observed = {
            "client": inv["client_name"], "tier": inv["tier"],
            "amount_inr": inv["amount_inr"],
            "outstanding_inr": inv["amount_inr"] - inv["amount_paid_inr"],
            "terms_days": inv["terms_days"],
            "days_past_terms": inv["days_past_terms"],
            "within_terms": inv["days_past_terms"] < 0,
            "po_matched": inv["po_matched"],
            "contact_verified": inv["contact_verified"],
            "status": inv["status"],
            "promise_status": inv["promise_status"],
            "contacts_sent": contacts,
            "at_risk": flagged, "risk_score": risk, "reason": reason,
        }

        if write_events:
            write_event(
                conn, invoice_id=inv["invoice_id"], client_id=inv["client_id"],
                stage="detect", observed=observed,
                decision="flag_at_risk" if flagged else "leave_alone",
                rule_applied="days_past_terms + amount + tier + contacts + promise",
                outcome=("at_risk" if flagged else "left_alone"),
            )

        entry = {"invoice_id": inv["invoice_id"], "client_id": inv["client_id"],
                 "risk": risk, "reason": reason, **observed}
        (at_risk if flagged else left_alone).append(entry)

    at_risk.sort(key=lambda e: e["risk"], reverse=True)
    return {"considered": len(rows), "at_risk": at_risk, "left_alone": left_alone}


def _report(conn, result):
    # Restraint scoring reads ground truth (allowed for reporting, not deciding).
    should_contact = {
        r[0]: r[1] for r in
        conn.execute("SELECT invoice_id, gt_should_contact FROM invoices")
    }
    left = result["left_alone"]
    correct = sum(1 for e in left if should_contact.get(e["invoice_id"]) == 0)
    settled = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE status = 'paid'").fetchone()[0]

    print(f"Detect ran over {result['considered']} open invoices "
          f"({settled} settled invoices skipped)\n")
    print(f"  Flagged at risk : {len(result['at_risk'])}")
    print(f"  Left alone      : {len(left)}  "
          f"({correct} correct - ground truth says don't contact the client)")

    misses = [e["invoice_id"] for e in left
              if should_contact.get(e["invoice_id"]) == 1]
    if misses:
        print(f"  Left-alone misses: {', '.join(misses)} "
              f"(signal lives only in the reply text - diagnose's job)")

    print("\n  Top 5 by risk:")
    print(f"    {'invoice':<9}{'client':<22}{'tier':<7}{'risk':>7}   why")
    for e in result["at_risk"][:5]:
        print(f"    {e['invoice_id']:<9}{e['client']:<22}{e['tier']:<7}"
              f"{e['risk']:>7}   {e['reason']}")


def main():
    conn = connect()
    try:
        result = run(conn)
        _report(conn, result)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
