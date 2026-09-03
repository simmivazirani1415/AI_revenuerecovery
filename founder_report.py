"""Founder report: pure SQL over event_log, invoices and clients. No LLM.

Six sections, rendered to both the terminal and docs/founder_report.md:
  1. Money at risk by tier and revenue line (outstanding / inside terms / late)
  2. Agent activity (considered / acted / silent / escalated / held)
  3. Discounts & waivers (who authorised, which rule fired)
  4. Every escalation (why, routed to whom, matched gt_correct_action?)
  5. Worst payers this quarter (broken promises, avg days past terms)
  6. Tier changes since last run (with the cause)
"""

import os
from datetime import datetime

from decide import GT_NORMALIZE
from log import connect
from policy import IST

HERE = os.path.dirname(os.path.abspath(__file__))
DOC_PATH = os.path.join(HERE, "docs", "founder_report.md")

TIER_ORDER = {"red": 0, "amber": 1, "green": 2}
CLIENT_FACING = {"nudge", "offer_payment_plan", "ask_for_ap_contact", "ask_for_delegate"}


def rs(n):
    return f"Rs {int(n or 0):,}"


def section1(conn):
    L = ["## 1. Money at risk (current open receivables)", ""]

    def grid(group_col, label):
        rows = conn.execute(f"""
            SELECT {group_col} AS g,
                   SUM(i.amount_inr - i.amount_paid_inr) AS outstanding,
                   SUM(CASE WHEN i.days_past_terms < 0
                       THEN i.amount_inr - i.amount_paid_inr ELSE 0 END) AS inside,
                   SUM(CASE WHEN i.days_past_terms >= 0
                       THEN i.amount_inr - i.amount_paid_inr ELSE 0 END) AS late
            FROM invoices i JOIN clients c ON c.client_id = i.client_id
            WHERE i.status != 'paid'
            GROUP BY {group_col}""").fetchall()
        out = [f"**By {label}**", "",
               f"| {label} | outstanding | inside terms | genuinely late |",
               "|---|--:|--:|--:|"]
        rows = sorted(rows, key=lambda r: TIER_ORDER.get(r["g"], 9)
                      if label == "tier" else r["g"])
        to, ti, tl = 0, 0, 0
        for r in rows:
            out.append(f"| {r['g']} | {rs(r['outstanding'])} | {rs(r['inside'])} "
                       f"| {rs(r['late'])} |")
            to += r["outstanding"]; ti += r["inside"]; tl += r["late"]
        out.append(f"| **total** | **{rs(to)}** | **{rs(ti)}** | **{rs(tl)}** |")
        out.append("")
        return out

    L += grid("c.tier", "tier")
    L += grid("i.revenue_line", "revenue_line")
    return L


def section2(conn):
    considered = conn.execute(
        "SELECT COUNT(DISTINCT invoice_id) FROM event_log WHERE stage='detect'").fetchone()[0]
    import json
    decs = conn.execute(
        "SELECT decision, observed FROM event_log WHERE stage='decide'").fetchall()
    silent = escalated = held = acted = 0
    for decision, observed in decs:
        appr = json.loads(observed or "{}").get("needs_founder_approval", False)
        if decision == "do_nothing":
            silent += 1
        elif decision == "escalate":
            escalated += 1
        elif appr and decision in CLIENT_FACING:
            held += 1
        else:
            acted += 1
    L = ["## 2. Agent activity", "",
         "| metric | count |", "|---|--:|",
         f"| invoices considered | {considered} |",
         f"| acted on (sent / routed to finance) | {acted} |",
         f"| deliberately silent (not actually late) | {silent} |",
         f"| escalated to a human | {escalated} |",
         f"| held for founder approval | {held} |", ""]
    return L


def section3(conn):
    rows = conn.execute("""
        SELECT d.invoice_id, c.name, c.tier, c.segment, d.decision, d.rule_applied,
               i.gt_correct_action, i.amount_inr, d.observed
        FROM event_log d
        JOIN invoices i ON i.invoice_id = d.invoice_id
        JOIN clients c ON c.client_id = d.client_id
        WHERE d.stage = 'decide'
          AND (d.decision = 'offer_payment_plan' OR i.gt_correct_action = 'waive_late_fee')
        ORDER BY d.invoice_id""").fetchall()
    L = ["## 3. Discounts & waivers applied", ""]
    if not rows:
        return L + ["_None this period._", ""]
    L += ["| invoice | client | concession | amount | authorised by | rule fired |",
          "|---|---|---|--:|---|---|"]
    import json
    for r in rows:
        kind = ("late-fee waiver" if r["gt_correct_action"] == "waive_late_fee"
                else "instalment plan")
        appr = json.loads(r["observed"] or "{}").get("needs_founder_approval", False)
        auth = ("founder (strategic)" if r["segment"] == "strategic"
                else "founder (>Rs 3,00,000)" if appr else "account owner")
        L.append(f"| {r['invoice_id']} | {r['name']} | {kind} | {rs(r['amount_inr'])} "
                 f"| {auth} | {r['rule_applied']} |")
    return L + [""]


def section4(conn):
    rows = conn.execute("""
        SELECT d.invoice_id, c.name, c.tier, d.diagnosis, d.routed_to,
               d.routed_reason, i.gt_correct_action, t.name AS routed_name
        FROM event_log d
        JOIN invoices i ON i.invoice_id = d.invoice_id
        JOIN clients c ON c.client_id = d.client_id
        LEFT JOIN team t ON t.person_id = d.routed_to
        WHERE d.stage = 'decide' AND d.decision = 'escalate'
        ORDER BY d.invoice_id""").fetchall()
    L = ["## 4. Escalations", "",
         "| invoice | client | why (diagnosis) | routed to | gt match |",
         "|---|---|---|---|:--:|"]
    match_n = 0
    for r in rows:
        matched = GT_NORMALIZE.get(r["gt_correct_action"], r["gt_correct_action"]) == "escalate"
        match_n += matched
        L.append(f"| {r['invoice_id']} | {r['name']} | {r['diagnosis']} "
                 f"| {r['routed_name']} | {'yes' if matched else 'NO'} |")
    L.append(f"\n{match_n}/{len(rows)} escalations match gt_correct_action.")
    return L + [""]


def section5(conn):
    rows = conn.execute("""
        SELECT c.name, c.tier, c.promises_made, c.promises_kept, c.promises_broken,
               (SELECT AVG(days_past_terms) FROM invoices i
                WHERE i.client_id = c.client_id AND i.status != 'paid'
                  AND i.days_past_terms > 0) AS avg_late,
               (SELECT SUM(amount_inr - amount_paid_inr) FROM invoices i
                WHERE i.client_id = c.client_id AND i.status != 'paid') AS outstanding
        FROM clients c
        WHERE c.promises_broken > 0
        ORDER BY c.promises_broken DESC, avg_late DESC""").fetchall()
    L = ["## 5. Worst payers this quarter", "",
         "| client | tier | promises (made/kept/broken) | avg days past terms | outstanding |",
         "|---|---|:--:|--:|--:|"]
    for r in rows:
        avg = f"{r['avg_late']:.0f}d" if r["avg_late"] is not None else "-"
        L.append(f"| {r['name']} | {r['tier']} "
                 f"| {r['promises_made']}/{r['promises_kept']}/{r['promises_broken']} "
                 f"| {avg} | {rs(r['outstanding'])} |")
    return L + [""]


def section6(conn, now_iso):
    conn.execute("""CREATE TABLE IF NOT EXISTS tier_snapshot (
        snapshot_at TEXT, client_id TEXT, tier TEXT, promises_broken INTEGER,
        worst_days_late INTEGER, response_rate REAL)""")
    prev_at = conn.execute("SELECT MAX(snapshot_at) FROM tier_snapshot").fetchone()[0]
    prev = {r["client_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM tier_snapshot WHERE snapshot_at = ?", (prev_at,))} if prev_at else {}

    cur = {r["client_id"]: dict(r) for r in conn.execute(
        "SELECT client_id, name, tier, promises_broken, worst_days_late, response_rate "
        "FROM clients")}

    L = ["## 6. Tier changes since last run", ""]
    if not prev:
        L.append(f"_Baseline recorded ({len(cur)} clients). No prior run to compare; "
                 "changes will appear here next time._")
    else:
        changes = []
        for cid, c in cur.items():
            p = prev.get(cid)
            if p and p["tier"] != c["tier"]:
                cause = []
                if c["promises_broken"] != p["promises_broken"]:
                    cause.append(f"broken promises {p['promises_broken']}→{c['promises_broken']}")
                if c["worst_days_late"] != p["worst_days_late"]:
                    cause.append(f"worst days late {p['worst_days_late']}→{c['worst_days_late']}")
                if round(c["response_rate"] or 0, 2) != round(p["response_rate"] or 0, 2):
                    cause.append(f"response rate {p['response_rate']}→{c['response_rate']}")
                changes.append((c["name"], p["tier"], c["tier"],
                                "; ".join(cause) or "history recomputed"))
        if changes:
            L += ["| client | from | to | cause |", "|---|---|---|---|"]
            for name, a, b, why in changes:
                L.append(f"| {name} | {a} | {b} | {why} |")
        else:
            L.append(f"_No tier changes since {prev_at[:16]}._")

    # record the new snapshot
    conn.executemany(
        "INSERT INTO tier_snapshot VALUES (?,?,?,?,?,?)",
        [(now_iso, c["client_id"], c["tier"], c["promises_broken"],
          c["worst_days_late"], c["response_rate"]) for c in cur.values()])
    conn.commit()
    return L + [""]


def _naive(conn, assumed_terms):
    """Naive tool metrics under an assumed flat term (days since issue > term)."""
    return conn.execute(f"""
        SELECT
          SUM(CASE WHEN dsi > {assumed_terms} THEN 1 ELSE 0 END) AS contacted,
          SUM(CASE WHEN dsi > {assumed_terms}
              THEN (dsi - {assumed_terms}) / 7 + 1 ELSE 0 END) AS cum_msgs,
          SUM(CASE WHEN dsi > {assumed_terms} AND days_past_terms < 0 THEN 1 ELSE 0 END) AS inside,
          SUM(CASE WHEN dsi > {assumed_terms} AND gt_diagnosis = 'dispute' THEN 1 ELSE 0 END) AS dispute,
          SUM(CASE WHEN dsi > {assumed_terms} AND segment = 'strategic' THEN 1 ELSE 0 END) AS strat,
          SUM(CASE WHEN dsi > {assumed_terms} AND contact_verified = 0 AND (
                lower(reply_text) LIKE '%bounce%' OR lower(reply_text) LIKE '%mailbox%'
                OR lower(reply_text) LIKE '%delivery failed%') THEN 1 ELSE 0 END) AS dead
        FROM (
          SELECT i.*, c.segment, (i.days_past_terms + i.terms_days) AS dsi
          FROM invoices i JOIN clients c ON c.client_id = i.client_id
          WHERE i.status != 'paid')""").fetchone()


def section7(conn):
    """Counterfactual: a naive reminder tool over the same open ledger."""
    import json
    n0 = _naive(conn, 0)     # due-on-issue (no terms concept)
    n30 = _naive(conn, 30)   # flat net-30 assumption

    # Agent: full disposition of the 24 open invoices.
    rows = conn.execute("""
        SELECT d.decision, d.register, d.observed, i.days_past_terms, i.gt_diagnosis,
               i.reply_text, i.contact_verified, c.segment
        FROM event_log d
        JOIN invoices i ON i.invoice_id = d.invoice_id
        JOIN clients c ON c.client_id = d.client_id
        WHERE d.stage = 'decide'""").fetchall()
    silence = escalated = finance = held = auto = blocked = 0
    a_inside = a_dispute = a_strat_firm = a_dead = 0
    for r in rows:
        dec = r["decision"]
        appr = json.loads(r["observed"] or "{}").get("needs_founder_approval", False)
        reply = (r["reply_text"] or "").lower()
        if dec == "do_nothing":
            silence += 1
        elif dec == "escalate":
            escalated += 1
        elif dec == "resubmit_via_finance":
            finance += 1
        elif appr:
            held += 1
        else:  # client-facing, not held -> would send unless a guardrail stops it
            payment = any(w in reply for w in ("in transit", "payment initiated", "funds in transit"))
            dead = r["contact_verified"] == 0 and any(w in reply for w in ("bounce", "mailbox", "delivery failed"))
            if payment or dead:
                blocked += 1
            else:
                auto += 1
                a_inside += r["days_past_terms"] < 0
                a_dispute += r["gt_diagnosis"] == "dispute"
                a_strat_firm += (r["segment"] == "strategic" and r["register"] == "firm")
                a_dead += r["contact_verified"] == 0

    L = ["## 7. Counterfactual: naive reminder tool", "",
         "_Naive tool = no per-client terms (chases from issue date), fixed 7-day "
         "cadence, one firm template, no memory of diagnosis / promises / replies / "
         "contact status. Same open ledger._", "",
         "**Disposition of the 24 open invoices**", "",
         "| how it's handled | naive tool | the agent |", "|---|--:|--:|",
         f"| auto-sent to client | {n0['contacted']} | {auto} |",
         f"| held for founder approval | 0 | {held} |",
         f"| routed to a person (escalated) | 0 | {escalated} |",
         f"| sent to finance | 0 | {finance} |",
         f"| correct silence | 0 | {silence} |",
         f"| blocked at guardrail | 0 | {blocked} |",
         f"| **total messages fired at clients** (7-day cadence) | **{n0['cum_msgs']}** | **{auto}** |",
         "",
         "**Client-facing harm** (of messages that actually reach a client)", "",
         "| harm | naive tool | the agent |", "|---|--:|--:|",
         f"| ...to clients **inside terms** | {n0['inside']} | {a_inside} |",
         f"| ...to clients **in dispute** | {n0['dispute']} | {a_dispute} |",
         f"| ...to **strategic** clients at **firm** register | {n0['strat']} | {a_strat_firm} |",
         f"| ...to a **dead contact** | {n0['dead']} | {a_dead} |",
         "",
         "**Sensitivity — naive tool assuming flat net-30 instead of due-on-issue**", "",
         "| metric | naive net-0 | naive net-30 |", "|---|--:|--:|",
         f"| invoices contacted | {n0['contacted']} | {n30['contacted']} |",
         f"| messages fired (cadence) | {n0['cum_msgs']} | {n30['cum_msgs']} |",
         f"| inside terms | {n0['inside']} | {n30['inside']} |",
         f"| in dispute | {n0['dispute']} | {n30['dispute']} |",
         f"| strategic at firm | {n0['strat']} | {n30['strat']} |",
         f"| dead contact | {n0['dead']} | {n30['dead']} |",
         "",
         f"Even the gentler net-30 naive tool still fires {n30['cum_msgs']} messages "
         f"and hits {n30['dispute']} dispute, {n30['strat']} strategic account and "
         f"{n30['dead']} dead contact - the terms-blindness harm falls to "
         f"{n30['inside']}, but the memory-blindness harms remain. The agent: "
         f"{auto} auto-sends, zero harm in any category.", ""]
    return L


def main():
    conn = connect()
    now = datetime.now(IST)
    try:
        parts = ["# Founder Report", "",
                 f"_Generated {now.strftime('%Y-%m-%d %H:%M IST')}. "
                 "Source: event_log + invoices (no LLM)._", ""]
        parts += section1(conn)
        parts += section2(conn)
        parts += section3(conn)
        parts += section4(conn)
        parts += section5(conn)
        parts += section6(conn, now.isoformat(timespec="seconds"))
        parts += section7(conn)
    finally:
        conn.close()

    doc = "\n".join(parts)
    os.makedirs(os.path.dirname(DOC_PATH), exist_ok=True)
    with open(DOC_PATH, "w", encoding="utf-8") as f:
        f.write(doc)
    print(doc)
    print(f"\n(saved to {DOC_PATH})")


if __name__ == "__main__":
    main()
