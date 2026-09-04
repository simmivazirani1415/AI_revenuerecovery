"""Decide stage: apply the decision table to each diagnosed invoice.

Pure rules, no LLM. Reads each invoice's diagnosis from the diagnose stage
(event_log) plus the invoice fields and client profile, and produces:
  action, register (+reason), routed_to (+reason), rule_applied,
  and whether it needs founder approval.

Override rules, applied strictly in this order, beat the base table:
  1. dispute                 -> stop, escalate to the service line's project lead
  2. not_actually_late       -> do nothing
  3. open promise not yet due -> wait, do nothing
  4. contact cap reached     -> escalate, don't send
  5. strategic client        -> cap 2 contacts, warm only, route Snehal early
"""

import os
from datetime import date

from log import connect, write_event
from policy import (CONTACT_CAP, FOUNDER_THRESHOLD_INR, REFERRAL_PATIENCE_DAYS,
                    REFERRALS_WARMER_THRESHOLD)

REF = date(2026, 9, 1)          # dataset "now"

_WARMTH = {"warm": 0, "neutral": 1, "firm": 2}
_WARMTH_INV = {0: "warm", 1: "neutral", 2: "firm"}


def _warmer(reg):
    return _WARMTH_INV[max(0, _WARMTH.get(reg, 1) - 1)]


def _next_up(pid):
    """Escalation ladder: owner/lead -> Snehal -> Aditya."""
    if pid == "aditya":
        return "aditya"
    if pid == "snehal":
        return "aditya"
    return "snehal"


def apply_referral(d, inv, names):
    """Founder-set referral policy. Mutates and returns the decision dict, and
    names the rule in register_reason / routed_reason (not just as fields)."""
    refmade = inv.get("referrals_made") or 0
    refby = inv.get("referred_by")

    if refmade >= REFERRALS_WARMER_THRESHOLD:
        d["register"] = _warmer(d["register"])
        d["register_reason"] += (
            f"; referrer of {refmade} clients -> one notch warmer, "
            f"+{REFERRAL_PATIENCE_DAYS}d patience before first contact")

    if refby:
        rname = names.get(refby, refby)
        d["register"] = _warmer(d["register"])
        d["register_reason"] += f"; referred by {rname} -> softer register"
        if d["routed_to"]:
            if d["action"] == "escalate":
                up = _next_up(d["routed_to"])
                if up != d["routed_to"]:
                    d["routed_to"] = up
                    d["routed_reason"] += (f"; referred by {rname} -> escalate one step "
                                           f"earlier (to {names.get(up, up)})")
                else:
                    d["routed_reason"] += (f"; referred by {rname} -> escalate one step "
                                           f"earlier (already at co-founder)")
            else:
                d["routed_reason"] += f"; referred by {rname}"
    return d

# Client-facing sends need founder sign-off above threshold / for strategic.
CLIENT_FACING = {"nudge", "offer_payment_plan", "ask_for_ap_contact", "ask_for_delegate",
                 "send_reauth_link"}

DELEGATE_HINTS = ("on leave", "delegate", "our own client", "their own client",
                  "waiting on our")

# gt_correct_action uses a finer vocabulary; fold it into the decide actions.
GT_NORMALIZE = {
    "wait_silence": "do_nothing", "waive_late_fee": "do_nothing",
    "fix_and_resubmit": "resubmit_via_finance", "send_reauth_link": "send_reauth_link",
    "draft_founder_sends": "nudge", "warm_nudge": "nudge",
    "warm_nudge_cap_two": "nudge", "chase_balance_only": "nudge",
    "request_ap_contact": "ask_for_ap_contact",
    "ask_once_then_escalate_flag_contact": "ask_for_ap_contact",
    "request_delegate_no_extension": "ask_for_delegate",
    "hold_route_to_lead": "escalate", "escalate": "escalate",
    "escalate_no_plan": "escalate", "escalate_at_contact_two": "escalate",
    "stop_route_founder": "escalate", "offer_payment_plan": "offer_payment_plan",
}


def _register(diagnosis, tier, strategic):
    if strategic:
        return "warm", "strategic account - protect the relationship"
    if tier == "red":
        return "firm", "red tier - firm register"
    if diagnosis == "cash_stress":
        return "warm", "genuine cash difficulty - don't pressure"
    if diagnosis == "stalling":
        return "firm", "stalling - firm register"
    if tier == "green":
        return "warm", "green tier - warm register"
    return "neutral", "amber tier, routine handling"


def decide(inv, diagnosis, contacts_sent):
    tier = inv["tier"]
    strategic = inv["segment"] == "strategic"
    owner = inv["internal_owner_id"]
    amount = inv["amount_inr"]
    reply = (inv["reply_text"] or "").lower()

    register, register_reason = _register(diagnosis, tier, strategic)
    needs_founder = amount > FOUNDER_THRESHOLD_INR or strategic

    def out(action, routed_to, routed_reason, rule):
        reg, reg_reason = register, register_reason
        # Override 5 forces warm register for strategic accounts.
        return {
            "action": action, "register": reg, "register_reason": reg_reason,
            "routed_to": routed_to, "routed_reason": routed_reason,
            "rule_applied": rule, "needs_founder_approval": needs_founder,
        }

    # --- Overrides, in strict order --------------------------------------
    # 1. dispute
    if diagnosis == "dispute":
        return out("escalate", owner,
                   f"dispute -> project lead for the service line ({owner}), copy Snehal",
                   "override 1: dispute -> stop + escalate to project lead")

    # 2. not_actually_late
    if diagnosis == "not_actually_late":
        return out("do_nothing", None, "nobody acts - inside terms / known freeze",
                   "override 2: not_actually_late -> do nothing")

    # 3. open promise not yet due
    if inv["promise_status"] == "pending" and inv["promise_date"] \
            and date.fromisoformat(inv["promise_date"]) > REF:
        return out("do_nothing", None,
                   f"promise pending until {inv['promise_date']} - wait it out",
                   "override 3: open promise not yet due -> wait")

    # 4. contact cap reached
    cap = CONTACT_CAP["strategic" if strategic else "standard"]
    if contacts_sent >= cap:
        return out("escalate", "aditya" if tier == "red" else "snehal",
                   f"{contacts_sent} contacts sent (cap {cap}) - stop sending, escalate",
                   f"override 4: contact cap ({cap}) reached -> escalate")

    # 5. strategic client
    if strategic:
        action = "nudge"
        if diagnosis == "process_block":
            action = "resubmit_via_finance"
        elif diagnosis == "wrong_contact":
            action = "ask_for_ap_contact"
        elif diagnosis == "cash_stress":
            action = "offer_payment_plan"
        elif diagnosis == "stalling":
            action = "escalate"
        return out(action, "snehal",
                   "strategic escalation -> Snehal, one step earlier than standard",
                   "override 5: strategic -> warm, cap 2, route Snehal early")

    # --- Base decision table ---------------------------------------------
    if diagnosis == "process_block":
        debit = inv.get("payment_failed") or any(w in reply for w in ("auto-debit", "debit", "card"))
        if debit:
            return out("send_reauth_link", "meera",
                       "failed auto-debit/renewal -> Product Lead sends a re-authorisation link",
                       "table: process_block (payment failed) -> send re-auth link")
        return out("resubmit_via_finance", "rhea",
                   "PO/portal/docs issue -> Finance Controller",
                   "table: process_block -> resubmit via finance")

    if diagnosis == "approver_bottleneck":
        if any(h in reply for h in DELEGATE_HINTS):
            return out("ask_for_delegate", owner,
                       f"approver unavailable -> account manager ({owner}) asks for a delegate",
                       "table: approver_bottleneck -> ask for delegate")
        return out("nudge", owner,
                   f"awaiting sign-off -> account manager ({owner}) nudges",
                   "table: approver_bottleneck -> nudge")

    if diagnosis == "wrong_contact":
        return out("ask_for_ap_contact", owner,
                   f"never reached AP -> account manager ({owner}) asks for the AP contact",
                   "table: wrong_contact -> ask for AP contact")

    if diagnosis == "cash_stress":
        if tier == "red":
            return out("escalate", "aditya",
                       "red-tier cash stress -> escalate to co-founder, no plan offered",
                       "table: cash_stress + red -> escalate")
        return out("offer_payment_plan", owner,
                   f"genuine cash stress -> {owner} offers the founder's payment-plan template",
                   "table: cash_stress -> offer payment plan")

    if diagnosis == "stalling":
        if "stop chasing" in reply or "absurd" in reply or "ridiculous" in reply:
            return out("escalate", "aditya",
                       "hostile reply -> route to co-founder, no de-escalation",
                       "table: stalling (hostile) -> escalate to co-founder")
        route = "aditya" if tier == "red" else owner
        return out("escalate", route,
                   f"stalling -> escalate ({'co-founder, red handover' if tier=='red' else owner})",
                   "table: stalling -> escalate")

    if diagnosis == "partial_payment":
        return out("nudge", owner,
                   f"partial payment received -> {owner} chases only the balance",
                   "table: partial_payment -> nudge for the balance")

    # Safety net (should not happen for a valid diagnosis)
    return out("escalate", "snehal", "unrecognised diagnosis -> escalate for review",
               "table: fallback -> escalate")


def run(conn):
    conn.execute("DELETE FROM event_log WHERE stage = 'decide'")
    conn.commit()

    # Default: consume the diagnose stage (the real pipeline). DECIDE_FROM_GT=1
    # feeds the ground-truth diagnoses instead, to test the decide rules in
    # isolation from any upstream diagnose error.
    if os.getenv("DECIDE_FROM_GT"):
        diag = {r[0]: r[1] for r in conn.execute(
            "SELECT invoice_id, gt_diagnosis FROM invoices WHERE gt_diagnosis IS NOT NULL")}
        print("[input: ground-truth diagnoses - decide rules in isolation]\n")
    else:
        diag = {r[0]: r[1] for r in conn.execute(
            "SELECT invoice_id, diagnosis FROM event_log WHERE stage = 'diagnose'")}
        if not diag:
            raise SystemExit("No diagnose events found - run diagnose.py first.")

    rows = [dict(r) for r in conn.execute(
        """SELECT ai.*, c.tier, c.segment, c.internal_owner_id, c.name AS client_name,
                  c.referred_by, c.referrals_made
           FROM agent_invoices ai JOIN clients c ON c.client_id = ai.client_id
           WHERE ai.status != 'paid' ORDER BY ai.invoice_id""")]
    names = {r[0]: r[1] for r in conn.execute("SELECT client_id, name FROM clients")}

    results = []
    for inv in rows:
        inv_id = inv["invoice_id"]
        diagnosis = diag.get(inv_id, "stalling")
        contacts = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE invoice_id = ? AND stage = 'execute' "
            "AND channel IS NOT NULL AND channel NOT IN ('none','internal')",
            (inv_id,)).fetchone()[0]

        d = apply_referral(decide(inv, diagnosis, contacts), inv, names)
        write_event(
            conn, invoice_id=inv_id, client_id=inv["client_id"], stage="decide",
            diagnosis=diagnosis, decision=d["action"], rule_applied=d["rule_applied"],
            register=d["register"], register_reason=d["register_reason"],
            routed_to=d["routed_to"], routed_reason=d["routed_reason"],
            observed={"tier": inv["tier"], "segment": inv["segment"],
                      "amount_inr": inv["amount_inr"],
                      "needs_founder_approval": d["needs_founder_approval"],
                      "diagnosis": diagnosis, "contacts_sent": contacts})
        results.append({**d, "invoice_id": inv_id, "client": inv["client_name"],
                        "tier": inv["tier"], "diagnosis": diagnosis,
                        "amount_inr": inv["amount_inr"],
                        "strategic": inv["segment"] == "strategic"})
    return results


def _report(conn, results):
    print(f"{'invoice':<8}{'client':<22}{'tier':<6}{'diagnosis':<20}"
          f"{'action':<21}{'reg':<8}{'route':<8}{'appr':<5}rule_applied")
    for r in sorted(results, key=lambda x: x["invoice_id"]):
        appr = "yes" if r["needs_founder_approval"] else "-"
        print(f"{r['invoice_id']:<8}{r['client'][:21]:<22}{r['tier']:<6}"
              f"{r['diagnosis']:<20}{r['action']:<21}{r['register']:<8}"
              f"{str(r['routed_to'] or '-'):<8}{appr:<5}{r['rule_applied']}")
    print(f"  (appr = needs founder approval: above Rs {FOUNDER_THRESHOLD_INR:,} "
          f"or strategic client)")

    need = sorted([r for r in results if r["needs_founder_approval"]],
                  key=lambda x: x["invoice_id"])
    print(f"\nNeed founder approval: {len(need)}/{len(results)}")
    for r in need:
        why = []
        if r["amount_inr"] > FOUNDER_THRESHOLD_INR:
            why.append(f"Rs {r['amount_inr']:,} > threshold")
        if r["strategic"]:
            why.append("strategic")
        print(f"  {r['invoice_id']:<8}{r['client'][:21]:<22}{' + '.join(why)}")

    gt = {r[0]: r[1] for r in conn.execute(
        "SELECT invoice_id, gt_correct_action FROM invoices "
        "WHERE gt_correct_action IS NOT NULL")}
    pred = {r["invoice_id"]: r["action"] for r in results}
    match = [i for i in gt if pred.get(i) == GT_NORMALIZE.get(gt[i], gt[i])]
    print(f"\nDecisions matching gt_correct_action: {len(match)}/{len(gt)} "
          f"= {100*len(match)/len(gt):.1f}%  (gt folded into the decide vocabulary)")
    misses = [(i, gt[i], pred.get(i)) for i in gt
              if pred.get(i) != GT_NORMALIZE.get(gt[i], gt[i])]
    if misses:
        print("Mismatches:")
        for i, g, p in sorted(misses):
            print(f"  {i}: gt={g} (-> {GT_NORMALIZE.get(g, g)})  decide={p}")


def main():
    conn = connect()
    try:
        _report(conn, run(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
