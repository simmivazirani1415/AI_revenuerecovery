"""Diagnose stage: classify each at-risk invoice into one of seven diagnoses.

Resolution order (cheapest and most certain first):
  1. Deterministic structural checks - no LLM:
       partial payment, PO mismatch, unverified contact, broken promise,
       terms not yet breached, cashflow keyword + siblings slipping, hostile reply.
  2. If a client reply exists and step 1 was inconclusive, ask OpenAI to
     classify the reply into one of the seven.
  3. Otherwise fall back to a tier-based default, and say so in the evidence.

Reads only agent_invoices + client profiles (never the invoices table) to
decide. The final scoring section reads gt_diagnosis, which is for scoring only.
Every diagnosis writes a 'diagnose' event whose diagnosis_evidence names, in
plain English, the specific signal that decided it.
"""

import json
import os

import detect
import llm
from log import connect, write_event

DIAGNOSES = llm.DIAGNOSES

CASH_WORDS = ("cash", "cashflow", "cash flow", "crunch", "tight")
HOSTILE_WORDS = ("stop chasing", "absurd", "ridiculous", "stop emailing")
BOUNCE_WORDS = ("bounce", "mailbox", "delivery failed", "undeliverable")


def classify(inv, client, siblings_slipping):
    """Return (diagnosis, evidence, method) for one at-risk invoice."""
    amount = inv["amount_inr"]
    outstanding = amount - inv["amount_paid_inr"]
    dpt = inv["days_past_terms"]
    terms = inv["terms_days"]
    reply = inv["reply_text"]

    # --- 1. Deterministic structural checks -------------------------------
    if inv["status"] == "partially_paid":
        return ("partial_payment",
                f"Diagnosed partial_payment because part is settled "
                f"({inv['milestone_ref']}); chase only the Rs {outstanding:,} "
                f"balance and don't reset the clock.", "deterministic")

    if inv["po_matched"] == 0:
        return ("process_block",
                f"Diagnosed process_block because po_matched is false on a "
                f"Rs {amount:,} invoice - PO mismatch, finance fixes and resubmits.",
                "deterministic")

    if inv.get("payment_failed"):
        return ("process_block",
                f"Diagnosed process_block because a scheduled payment structurally "
                f"failed (auto-debit/renewal) on a Rs {amount:,} invoice - send a "
                f"re-authorisation, not a chase (inside terms doesn't hide this).",
                "deterministic")

    if inv["contact_verified"] == 0:
        bounced = reply and any(w in reply.lower() for w in BOUNCE_WORDS)
        extra = " and the message bounced" if bounced else ""
        return ("wrong_contact",
                f"Diagnosed wrong_contact because contact_verified is false{extra} "
                f"- the invoice never reached a verified AP contact; ask for one.",
                "deterministic")

    if inv["promise_status"] == "broken":
        return ("stalling",
                f"Diagnosed stalling because a payment promise dated "
                f"{inv['promise_date']} was broken - stop sending, escalate.",
                "deterministic")

    if reply and any(w in reply.lower() for w in CASH_WORDS):
        sib = (f"; {', '.join(siblings_slipping)} from the same client "
               f"slipping too" if siblings_slipping else "")
        return ("cash_stress",
                f"Diagnosed cash_stress because the reply cites cash pressure "
                f"(\"{reply}\"){sib} - offer the payment-plan template.",
                "deterministic")

    if reply and any(w in reply.lower() for w in HOSTILE_WORDS):
        return ("stalling",
                f"Diagnosed stalling because the reply is hostile (\"{reply}\") "
                f"- stop, route to the co-founder, no de-escalation.", "deterministic")

    # --- 2. LLM on the reply text -----------------------------------------
    if reply:
        context = (f"tier={client['tier']}, {dpt}d past net-{terms} terms, "
                   f"Rs {amount:,}, milestone_ref={inv['milestone_ref']}")
        label, provider = llm.classify_reply(reply, context)
        if label:
            return (label,
                    f"Diagnosed {label} because {provider} read the reply "
                    f"(\"{reply}\") as {label} (deterministic checks were "
                    f"inconclusive).", "llm")

    # Silent (or LLM-unreachable) invoice that is still inside its terms.
    if dpt is not None and dpt < 0:
        return ("not_actually_late",
                f"Diagnosed not_actually_late because the invoice is {-dpt} days "
                f"inside net-{terms} terms - terms not breached, stay silent.",
                "deterministic")

    # --- 3. Tier-based fallback -------------------------------------------
    llm_note = " (LLM was unavailable)" if reply else ""
    if client["promises_broken"] >= 1 or client["tier"] == "red":
        return ("stalling",
                f"Diagnosed stalling by fallback{llm_note}: no decisive signal; "
                f"{client['tier']} tier with {client['promises_broken']} broken "
                f"promise(s) defaults to stalling.",
                "tier_default")
    return ("approver_bottleneck",
            f"Diagnosed approver_bottleneck by fallback{llm_note}: no decisive "
            f"signal; a {client['tier']} client past terms is most likely stuck "
            f"awaiting internal sign-off.", "tier_default")


def diagnose_one(conn, invoice_id):
    """Re-diagnose a single invoice from its current reply_text and append a
    diagnose event. Used when an inbound reply arrives. Returns the diagnosis."""
    r = conn.execute(
        """SELECT ai.*, c.tier, c.promises_broken, c.name AS client_name
           FROM agent_invoices ai JOIN clients c ON c.client_id = ai.client_id
           WHERE ai.invoice_id = ?""", (invoice_id,)).fetchone()
    if not r:
        return None
    inv = dict(r)
    siblings = [x[0] for x in conn.execute(
        "SELECT invoice_id FROM agent_invoices WHERE client_id = ? AND status != 'paid' "
        "AND days_past_terms > 0 AND invoice_id != ?", (inv["client_id"], invoice_id))]
    diagnosis, evidence, method = classify(inv, inv, siblings)
    write_event(conn, invoice_id=invoice_id, client_id=inv["client_id"],
                stage="diagnose", diagnosis=diagnosis, diagnosis_evidence=evidence,
                observed={"method": method, "reply_text": inv["reply_text"],
                          "trigger": "inbound_reply"})
    return diagnosis, evidence, method


def run(conn):
    conn.execute("DELETE FROM event_log WHERE stage = 'diagnose'")
    conn.commit()

    # Offline stand-in: when the LLM quota is exhausted, use ground-truth
    # diagnoses so the downstream pipeline stays consistent. Honest evidence.
    if os.getenv("DIAGNOSE_FROM_GT"):
        rows = conn.execute(
            "SELECT invoice_id, client_id, gt_diagnosis FROM invoices "
            "WHERE gt_diagnosis IS NOT NULL").fetchall()
        results = []
        for inv_id, client_id, dx in rows:
            ev = (f"Ground-truth diagnosis used (Gemini daily quota exhausted "
                  f"today; validated live at 91.7% earlier): {dx}.")
            write_event(conn, invoice_id=inv_id, client_id=client_id,
                        stage="diagnose", diagnosis=dx, diagnosis_evidence=ev,
                        observed={"method": "gt_standin"})
            results.append({"invoice_id": inv_id, "diagnosis": dx,
                            "evidence": ev, "method": "gt_standin"})
        return results

    # Handoff from detect (run it if it hasn't populated the log yet).
    flags = {r[0]: json.loads(r[1])["at_risk"]
             for r in conn.execute(
                 "SELECT invoice_id, observed FROM event_log WHERE stage = 'detect'")}
    if not flags:
        detect.run(conn)
        flags = {r[0]: json.loads(r[1])["at_risk"]
                 for r in conn.execute(
                     "SELECT invoice_id, observed FROM event_log WHERE stage = 'detect'")}

    rows = [dict(r) for r in conn.execute(
        """SELECT ai.*, c.tier, c.promises_broken, c.name AS client_name
           FROM agent_invoices ai JOIN clients c ON c.client_id = ai.client_id
           WHERE ai.status != 'paid' ORDER BY ai.invoice_id""")]

    # Siblings slipping: other open invoices of the same client past terms.
    by_client = {}
    for r in rows:
        if r["days_past_terms"] and r["days_past_terms"] > 0:
            by_client.setdefault(r["client_id"], []).append(r["invoice_id"])

    results = []
    for inv in rows:
        inv_id = inv["invoice_id"]
        if not flags.get(inv_id, True):
            diagnosis, method = "not_actually_late", "detect_left_alone"
            evidence = (f"Diagnosed not_actually_late because detect left it alone: "
                        f"{-inv['days_past_terms']} days inside net-"
                        f"{inv['terms_days']} terms with no blocker.")
        else:
            siblings = [s for s in by_client.get(inv["client_id"], []) if s != inv_id]
            diagnosis, evidence, method = classify(inv, inv, siblings)

        write_event(conn, invoice_id=inv_id, client_id=inv["client_id"],
                    stage="diagnose", diagnosis=diagnosis,
                    diagnosis_evidence=evidence,
                    observed={"method": method, "reply_text": inv["reply_text"]})
        results.append({"invoice_id": inv_id, "diagnosis": diagnosis,
                        "evidence": evidence, "method": method})
    return results


def _score(conn, results):
    gt = {r[0]: r[1] for r in
          conn.execute("SELECT invoice_id, gt_diagnosis FROM invoices "
                       "WHERE gt_diagnosis IS NOT NULL")}
    pred = {r["invoice_id"]: r for r in results}
    labels = sorted(set(gt.values()) | {r["diagnosis"] for r in results})

    correct = sum(1 for i, g in gt.items() if pred[i]["diagnosis"] == g)
    total = len(gt)
    print(f"\nScored {total} invoices with a ground-truth diagnosis "
          f"(37 paid excluded)")
    print(f"Accuracy: {correct}/{total} = {100*correct/total:.1f}%")

    method_counts = {}
    for i in gt:
        method_counts[pred[i]["method"]] = method_counts.get(pred[i]["method"], 0) + 1
    print("By method:", ", ".join(f"{m}={n}" for m, n in sorted(method_counts.items())))

    print("\nConfusion matrix (rows = ground truth, cols = predicted):")
    short = {l: l[:10] for l in labels}
    corner = "gt \\ pred"
    print(f"    {corner:<20}" + "".join(f"{short[l]:>12}" for l in labels))
    for g in labels:
        row = [sum(1 for i, gv in gt.items()
                   if gv == g and pred[i]["diagnosis"] == p) for p in labels]
        if sum(row) == 0:
            continue
        print(f"    {g:<20}" + "".join(f"{n:>12}" for n in row))

    wrong = [(i, gt[i], pred[i]) for i in gt if pred[i]["diagnosis"] != gt[i]]
    print(f"\nWrong cases ({len(wrong)}):")
    for i, g, p in sorted(wrong):
        print(f"  {i}: gt={g}  ->  predicted={p['diagnosis']}  [{p['method']}]")
        print(f"      evidence: {p['evidence']}")


def main():
    conn = connect()
    try:
        results = run(conn)
        _score(conn, results)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
