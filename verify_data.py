"""Inspect the generated dataset and confirm it matches Dataset Spec v2.

This is a SCORING/inspection script, so it reads the raw invoices table
(including the gt_ ground-truth columns) directly. Agent code must never
do this — it reads the agent_invoices view instead.

Prints:
  1. Count per bucket (tier, revenue line, status, diagnosis)
  2. The 7x3 diagnosis x tier coverage grid, with empty cells named
  3. Which embedded edge cases are present
  4. How many open invoices currently sit inside their payment terms
"""

import os
import sqlite3

from policy import FOUNDER_THRESHOLD_INR   # single source of truth for the threshold

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "agency.db")

# The seven canonical diagnoses, in spec order.
DIAGNOSES = [
    "not_actually_late",
    "process_block",
    "approver_bottleneck",
    "wrong_contact",
    "dispute",
    "cash_stress",
    "stalling",
]
TIERS = ["green", "amber", "red"]


def rule(title):
    print("\n" + title)
    print("-" * len(title))


def count_by(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit("agency.db not found - run generate_data.py first.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    invoices = [dict(r) for r in conn.execute("SELECT * FROM invoices")]
    clients = [dict(r) for r in conn.execute("SELECT * FROM clients")]
    tier_of = {c["client_id"]: c["tier"] for c in clients}

    open_inv = [i for i in invoices if i["status"] != "paid"]
    actionable = [i for i in open_inv if i["gt_diagnosis"] in DIAGNOSES]

    print(f"Dataset: {len(invoices)} invoices, {len(clients)} clients")

    # -- 1. Counts per bucket ------------------------------------------------
    rule("1. Counts per bucket")

    print("By tier (clients):")
    tc = count_by(clients, "tier")
    for t in TIERS:
        print(f"    {t:<8} {tc.get(t, 0)}")

    print("By revenue line (invoices):")
    for line, n in sorted(count_by(invoices, "revenue_line").items()):
        print(f"    {line:<16} {n}")

    print("By status (invoices):")
    for st, n in sorted(count_by(invoices, "status").items()):
        print(f"    {st:<16} {n}")

    print(f"By diagnosis (actionable open invoices, {len(actionable)} total):")
    dc = count_by(actionable, "gt_diagnosis")
    for d in DIAGNOSES:
        print(f"    {d:<22} {dc.get(d, 0)}")
    other = {k: v for k, v in count_by(actionable, "gt_diagnosis").items()
             if k not in DIAGNOSES}
    for k, v in other.items():
        print(f"    {k+' (edge)':<22} {v}")

    # -- 2. Coverage grid ----------------------------------------------------
    rule("2. Coverage grid (diagnosis x tier)")

    grid = {(d, t): [] for d in DIAGNOSES for t in TIERS}
    for i in actionable:
        grid[(i["gt_diagnosis"], tier_of[i["client_id"]])].append(i["invoice_id"])

    header = f"{'diagnosis':<22}" + "".join(f"{t:<14}" for t in TIERS)
    print(header)
    empty_cells = []
    for d in DIAGNOSES:
        cells = []
        for t in TIERS:
            ids = grid[(d, t)]
            if ids:
                cells.append(f"{'#' + str(len(ids)) + ' ' + ','.join(ids):<14}")
            else:
                cells.append(f"{'.':<14}")
                empty_cells.append(f"{d}/{t}")
        print(f"{d:<22}" + "".join(cells))

    filled = 21 - len(empty_cells)
    print(f"\nFilled cells: {filled}/21   Empty cells: {len(empty_cells)}")
    print("Deliberately empty (see spec note - these combinations aren't believable):")
    for c in empty_cells:
        print(f"    {c}")

    # -- 3. Edge cases present ------------------------------------------------
    rule("3. Edge cases present")

    def has(pred):
        return [i["invoice_id"] for i in invoices if pred(i)]

    def reply(i, *subs):
        t = (i["reply_text"] or "").lower()
        return any(s in t for s in subs)

    dup_numbers = [n for n, c in
                   count_by(invoices, "invoice_number").items() if c > 1]
    duplicate_ids = [i["invoice_id"] for i in invoices
                     if i["invoice_number"] in dup_numbers]

    edge_cases = [
        ("Payment lands mid-sequence",
         has(lambda i: reply(i, "in transit", "initiated"))),
        ("Partial payment",
         has(lambda i: i["status"] == "partially_paid")),
        (f"Duplicate invoice number ({', '.join(dup_numbers) or 'none'})",
         duplicate_ids),
        ("Hostile reply",
         has(lambda i: reply(i, "stop chasing", "absurd", "ridiculous"))),
        ("Dead contact",
         has(lambda i: i["contact_verified"] == 0
             and reply(i, "delivery failed", "bounce", "mailbox"))),
        ("Wrong contact (unverified AP)",
         has(lambda i: i["gt_diagnosis"] == "wrong_contact")),
        ("Promise made and kept",
         has(lambda i: i["promise_status"] == "kept")),
        ("Promise made and broken",
         has(lambda i: i["promise_status"] == "broken")),
        ("Cash-stress signal",
         has(lambda i: reply(i, "cash"))),
        (f"Above approval threshold (Rs {FOUNDER_THRESHOLD_INR:,}), open",
         has(lambda i: i["status"] != "paid" and i["amount_inr"] > FOUNDER_THRESHOLD_INR)),
        ("Late-fee waiver earned (flagged before due)",
         has(lambda i: i["days_past_terms"] is not None
             and i["days_past_terms"] < 0 and reply(i, "may run", "may be late"))),
        ("Failed auto-debit",
         has(lambda i: reply(i, "auto-debit"))),
    ]

    strategic_ids = [i["invoice_id"] for i in open_inv
                     if next(c for c in clients
                             if c["client_id"] == i["client_id"])["segment"] == "strategic"]
    edge_cases.append(("Strategic client (open)", strategic_ids))

    for label, ids in edge_cases:
        mark = "OK " if ids else "-- "
        shown = ", ".join(ids[:6]) + (" ..." if len(ids) > 6 else "")
        print(f"  [{mark}] {label}: {len(ids)}  {shown}")
    print("  [n/a] Payment link fails to generate: injected at runtime, "
          "not stored in the dataset")

    # -- 4. Invoices inside their terms --------------------------------------
    rule("4. Restraint check - invoices inside their payment terms")

    inside = [i for i in open_inv
              if i["days_past_terms"] is not None and i["days_past_terms"] < 0]
    pct = round(100 * len(inside) / len(open_inv), 1) if open_inv else 0
    print(f"{len(inside)} of {len(open_inv)} open invoices are inside terms "
          f"({pct}% - spec target ~1/3). Chasing any of these would be wrong.")
    for i in sorted(inside, key=lambda r: r["invoice_id"]):
        print(f"    {i['invoice_id']:<8} {i['client_id']:<12} "
              f"net-{i['terms_days']:<3} {i['days_past_terms']:>4}d to due   "
              f"[{i['gt_diagnosis']}]")

    conn.close()


if __name__ == "__main__":
    main()
