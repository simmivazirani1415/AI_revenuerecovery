"""Build the receivables dataset from Dataset Spec v2.

Creates agency.db from db/schema.sql, then loads:
  - 8 team members
  - 15 clients
  - 61 invoices (INV-01..INV-61): 37 historical paid + 24 open/actionable

Client tier is COMPUTED from payment history (see compute_tier), never
hardcoded. Payment-behaviour metrics (invoices_paid, worst_days_late, etc.)
are derived from the actual invoice rows, so the tier follows the ledger.

Reference "today" is fixed at 2026-09-01 so the dataset is reproducible.
"""

import os
import sqlite3
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "agency.db")
SCHEMA_PATH = os.path.join(HERE, "db", "schema.sql")

REF = date(2026, 9, 1)  # deterministic "today"


def iso(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------------------
# Tier computation — the point of interest. Explained in the run summary.
# ---------------------------------------------------------------------------
def compute_tier(*, invoices_paid, promises_broken, worst_days_late, response_rate):
    """Derive green / amber / red from a client's track record.

    - No paid history yet  -> amber (unproven, can't be vouched green).
    - Two+ broken promises  -> red (the problem is intent, not paperwork).
    - Any of: one broken promise, chronic lateness (>10 days past terms in
      paid history), or a low response rate (<0.6) -> at least amber.
    - Amber escalates to red if lateness is severe (>=60 days) AND trust is
      already dented by a broken promise.
    - Otherwise -> green.
    """
    if invoices_paid == 0:
        return "amber"

    if promises_broken >= 2:
        return "red"

    amber_signal = (
        promises_broken >= 1
        or worst_days_late > 10
        or (response_rate is not None and response_rate < 0.6)
    )

    if amber_signal and promises_broken >= 1 and worst_days_late >= 60:
        return "red"

    return "amber" if amber_signal else "green"


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------
TEAM = [
    ("aditya", "Aditya Menon", "Co-founder", "Leadership",
     "above_threshold,hostile_reply,red_handover"),
    ("snehal", "Snehal Rao", "Head of Client Servicing", "Client Servicing",
     "relationship_risk,strategic_account,failed_unblock"),
    ("rhea", "Rhea Kapadia", "Finance Controller", "Finance",
     "process_block,po_mismatch,portal_rejection,gst_docs,credit_note"),
    ("karan", "Karan Bhatt", "Project Lead, Immersive Tech", "Immersive Tech",
     "milestone_dispute,scope_dispute"),
    ("nikhil", "Nikhil Sethi", "Project Lead, Experiential", "Experiential",
     "milestone_dispute,scope_dispute"),
    ("meera", "Meera Iyer", "Product Lead, V-Suite", "V-Suite",
     "licence_renewal,debit_failure,usage_block"),
    ("faiz", "Faiz Ahmed", "Delivery Manager, Mobile & Web", "Mobile & Web",
     "delivery_hold"),
    ("tanvi", "Tanvi Desai", "Account Manager, Real Estate", "Real Estate",
     "daily_realestate"),
]


# ---------------------------------------------------------------------------
# Clients — static attributes. tier + payment metrics filled in later.
# ---------------------------------------------------------------------------
def C(**kw):
    return kw


CLIENTS = [
    # --- Project work ---
    C(id="aureus", name="Aureus Motors", industry="automotive", line="project",
      segment="standard", terms=45, owner="nikhil", ap_name="Priya Nair",
      ap_verified=1, portal=None, years=3, revenue=2400000, resp=0.92,
      pm=0, pk=0, pb=0, off_topic=0),
    C(id="sable", name="Sable Properties", industry="real_estate", line="project",
      segment="strategic", terms=60, owner="tanvi", ap_name="Rohit Shah",
      ap_verified=1, portal=None, years=4, revenue=2200000, resp=0.88,
      pm=1, pk=1, pb=0, off_topic=0),
    C(id="nexora", name="Nexora Telecom", industry="telecom", line="project",
      segment="standard", terms=45, owner="karan", ap_name="Deepa Menon",
      ap_verified=1, portal="Coupa", years=2, revenue=1500000, resp=0.70,
      pm=0, pk=0, pb=0, off_topic=0),
    C(id="halcyon", name="Halcyon Pharma", industry="pharma", line="project",
      segment="standard", terms=45, owner="karan", ap_name="S. Iyer",
      ap_verified=1, portal="SAP Ariba", years=2, revenue=3200000, resp=0.90,
      pm=0, pk=0, pb=0, off_topic=0),
    C(id="brightline", name="Brightline Retail", industry="retail", line="project",
      segment="standard", terms=45, owner="faiz", ap_name="M. Khan",
      ap_verified=1, portal=None, years=3, revenue=1600000, resp=0.45,
      pm=0, pk=0, pb=0, off_topic=0),
    C(id="corvid", name="Corvid Sports Network", industry="sports_media", line="project",
      segment="standard", terms=45, owner="nikhil", ap_name=None,
      ap_verified=0, portal=None, years=2, revenue=4200000, resp=0.60,
      pm=1, pk=0, pb=1, off_topic=0),
    C(id="trellis", name="Trellis Infra", industry="infrastructure", line="project",
      segment="standard", terms=45, owner="karan", ap_name="V. Rao",
      ap_verified=1, portal=None, years=2, revenue=3000000, resp=0.30,
      pm=3, pk=0, pb=3, off_topic=1),

    # --- Product licence, the V-Suite ---
    C(id="wren", name="Wren Realty", industry="real_estate", line="product_licence",
      segment="standard", terms=30, owner="meera", ap_name="A. Pillai",
      ap_verified=1, portal=None, years=3, revenue=300000, resp=0.90,
      pm=0, pk=0, pb=0, off_topic=0),
    C(id="fable", name="Fable Studios", industry="media", line="product_licence",
      segment="standard", terms=30, owner="meera", ap_name="Team AP",
      ap_verified=1, portal="SAP Ariba", years=0.2, revenue=65000, resp=0.50,
      pm=0, pk=0, pb=0, off_topic=0),
    C(id="kestrel", name="Kestrel Events", industry="events", line="product_licence",
      segment="standard", terms=30, owner="meera", ap_name="N. Joshi",
      ap_verified=1, portal=None, years=2, revenue=400000, resp=0.35,
      pm=2, pk=0, pb=2, off_topic=1),
    C(id="amrit", name="Amrit Global", industry="conferences", line="product_licence",
      segment="standard", terms=30, owner="meera", ap_name="R. Verma",
      ap_verified=0, portal=None, years=2, revenue=540000, resp=0.20,
      pm=2, pk=0, pb=2, off_topic=0),
    C(id="lumen", name="Lumen Interiors", industry="interiors_retail", line="product_licence",
      segment="standard", terms=30, owner="meera", ap_name="K. Das",
      ap_verified=1, portal=None, years=2, revenue=340000, resp=0.78,
      pm=0, pk=0, pb=0, off_topic=0),

    # --- Retainer ---
    C(id="vantage", name="Vantage Consumer Group", industry="consumer_goods", line="retainer",
      segment="strategic", terms=15, owner="snehal", ap_name="L. Fernandes",
      ap_verified=1, portal=None, years=5, revenue=14400000, resp=0.95,
      pm=0, pk=0, pb=0, off_topic=0),
    C(id="saffron", name="Saffron Media House", industry="media", line="retainer",
      segment="standard", terms=15, owner="snehal", ap_name="P. Gupta",
      ap_verified=1, portal=None, years=2, revenue=3600000, resp=0.55,
      pm=1, pk=0, pb=1, off_topic=1),
    C(id="verma", name="Verma & Sons Developers", industry="real_estate", line="retainer",
      segment="standard", terms=15, owner="tanvi", ap_name="Verma Jr.",
      ap_verified=1, portal=None, years=2, revenue=4800000, resp=0.30,
      pm=2, pk=0, pb=2, off_topic=1),
]
CLIENT_BY_ID = {c["id"]: c for c in CLIENTS}

# Referral graph: Sable referred Wren, Halcyon and Kestrel; Vantage referred Saffron.
REFERRED_BY = {"wren": "sable", "halcyon": "sable", "kestrel": "sable",
               "saffron": "vantage"}
REFERRALS_MADE = {"sable": 3, "vantage": 1}


# ---------------------------------------------------------------------------
# Historical PAID invoices (fill the numbering gaps). issue_ago = days before
# REF; dtp = days from issue to payment.
# ---------------------------------------------------------------------------
def P(id, client, amount, issue_ago, dtp):
    return dict(id=id, client=client, amount=amount, issue_ago=issue_ago, dtp=dtp)


PAID = [
    # Aureus (net-45) — clean
    P("INV-01", "aureus", 500000, 150, 40),
    P("INV-02", "aureus", 750000, 120, 43),
    P("INV-03", "aureus", 620000, 95, 38),
    # Sable (net-60) — always ~day 50
    P("INV-08", "sable", 1400000, 140, 50),
    P("INV-09", "sable", 800000, 110, 52),
    P("INV-10", "sable", 650000, 85, 48),
    # Vantage (net-15) — 5-year anchor, reliable
    P("INV-06", "vantage", 240000, 150, 12),
    P("INV-07", "vantage", 240000, 120, 14),
    P("INV-53", "vantage", 240000, 90, 10),
    P("INV-54", "vantage", 240000, 60, 13),
    # Nexora (net-45) — erratic; second paid late (quarter-end)
    P("INV-12", "nexora", 680000, 140, 38),
    P("INV-13", "nexora", 540000, 100, 71),
    # Halcyon (net-45) — clean
    P("INV-16", "halcyon", 900000, 145, 40),
    P("INV-17", "halcyon", 1100000, 115, 44),
    P("INV-18", "halcyon", 850000, 80, 42),
    # Brightline (net-45) — one badly late, low response
    P("INV-20", "brightline", 350000, 148, 30),
    P("INV-21", "brightline", 420000, 118, 35),
    P("INV-22", "brightline", 380000, 130, 100),
    # Corvid (net-45) — large but slow
    P("INV-25", "corvid", 1500000, 150, 40),
    P("INV-26", "corvid", 1300000, 120, 55),
    P("INV-27", "corvid", 1400000, 90, 60),
    # Trellis (net-45) — chronic
    P("INV-30", "trellis", 900000, 150, 60),
    P("INV-31", "trellis", 1100000, 120, 75),
    P("INV-32", "trellis", 1000000, 100, 90),
    # Wren (net-30) — renewals on time
    P("INV-35", "wren", 90000, 150, 20),
    P("INV-36", "wren", 95000, 110, 25),
    P("INV-37", "wren", 100000, 70, 28),
    # Saffron (net-15) — creeping late
    P("INV-40", "saffron", 150000, 150, 20),
    P("INV-41", "saffron", 150000, 120, 25),
    P("INV-57", "saffron", 150000, 90, 30),
    # Kestrel (net-30) — late
    P("INV-43", "kestrel", 120000, 150, 55),
    P("INV-44", "kestrel", 130000, 120, 60),
    # Amrit (net-30) — very late
    P("INV-47", "amrit", 180000, 150, 65),
    P("INV-48", "amrit", 175000, 120, 70),
    # Lumen (net-30) — one early, one late
    P("INV-50", "lumen", 110000, 150, 8),
    P("INV-51", "lumen", 115000, 100, 44),
    # Verma (net-15) — thin history, late
    P("INV-59", "verma", 200000, 150, 60),
]


# ---------------------------------------------------------------------------
# OPEN / actionable invoices — carry the ground truth.
# ---------------------------------------------------------------------------
def O(**kw):
    kw.setdefault("status", "open")
    kw.setdefault("amount_paid", 0)
    kw.setdefault("po", None)
    kw.setdefault("po_matched", None)
    kw.setdefault("milestone", None)
    kw.setdefault("contact_verified", 1)
    kw.setdefault("reply", None)
    kw.setdefault("promise_date", None)
    kw.setdefault("promise_status", None)
    kw.setdefault("inv_number", None)
    return kw


OPEN = [
    # Aureus
    O(id="INV-04", client="aureus", amount=480000, issue_ago=12,
      gt_dx="not_actually_late", gt_action="wait_silence", gt_contact=0, gt_route=None),
    O(id="INV-05", client="aureus", amount=900000, issue_ago=20,
      po="PO-AUR-2213", po_matched=0,
      reply="Our PO shows a different amount than the invoice.",
      gt_dx="process_block", gt_action="fix_and_resubmit", gt_contact=0, gt_route="rhea"),

    # Sable — Rs 3.2L (above threshold), strategic, promise made & kept
    O(id="INV-11", client="sable", amount=320000, issue_ago=78,
      reply="We'll release payment by the 25th.",
      promise_date="2026-08-25", promise_status="kept",
      gt_dx="approver_bottleneck", gt_action="draft_founder_sends", gt_contact=1, gt_route="aditya"),

    # Nexora — quarter-end freeze (not late) + portal rejection (process block)
    O(id="INV-14", client="nexora", amount=720000, issue_ago=30,
      reply="Payments are frozen until quarter-end close.",
      gt_dx="not_actually_late", gt_action="wait_silence", gt_contact=0, gt_route=None),
    O(id="INV-15", client="nexora", amount=460000, issue_ago=30,
      inv_number="INV-2026-041",  # duplicate number, shared with Fable INV-42
      po="PO-NEX-8890", po_matched=0,
      reply="Invoice rejected on our AP portal - GST detail doesn't match.",
      gt_dx="process_block", gt_action="fix_and_resubmit", gt_contact=0, gt_route="rhea"),

    # Halcyon — milestone contested
    O(id="INV-19", client="halcyon", amount=1250000, issue_ago=30, milestone="M2",
      reply="Milestone 2 deliverable quality is contested.",
      gt_dx="dispute", gt_action="hold_route_to_lead", gt_contact=0, gt_route="karan"),

    # Brightline — cash stress, two invoices slipping together
    O(id="INV-23", client="brightline", amount=300000, issue_ago=60,
      reply="We're facing a cashflow crunch this quarter.",
      gt_dx="cash_stress", gt_action="offer_payment_plan", gt_contact=1, gt_route="faiz"),
    O(id="INV-24", client="brightline", amount=280000, issue_ago=55,
      reply="Same story, cash is tight right now.",
      gt_dx="cash_stress", gt_action="offer_payment_plan", gt_contact=1, gt_route="faiz"),

    # Corvid — wrong contact + partial payment
    O(id="INV-28", client="corvid", amount=1600000, issue_ago=70, contact_verified=0,
      gt_dx="wrong_contact", gt_action="request_ap_contact", gt_contact=1, gt_route="nikhil"),
    O(id="INV-29", client="corvid", amount=1800000, issue_ago=65,
      status="partially_paid", amount_paid=1200000, milestone="M1-M2 of 3",
      reply="We've paid for the two completed milestones.",
      gt_dx="partial_payment", gt_action="chase_balance_only", gt_contact=1, gt_route="nikhil"),

    # Trellis — stalling (broken promise) + late scope dispute
    O(id="INV-33", client="trellis", amount=1350000, issue_ago=116,
      promise_date="2026-08-10", promise_status="broken",
      reply="Will sort it out next week.",
      gt_dx="stalling", gt_action="escalate", gt_contact=0, gt_route="aditya"),
    O(id="INV-34", client="trellis", amount=1200000, issue_ago=80,
      reply="Actually, we dispute the scope on this one.",
      gt_dx="dispute", gt_action="hold_route_to_lead", gt_contact=0, gt_route="karan"),

    # Wren — renewal, payment landing mid-sequence
    O(id="INV-38", client="wren", amount=110000, issue_ago=34,
      reply="Payment initiated, funds in transit.",
      gt_dx="approver_bottleneck", gt_action="warm_nudge", gt_contact=1, gt_route="meera"),
    O(id="INV-39", client="wren", amount=110000, issue_ago=10,
      gt_dx="not_actually_late", gt_action="wait_silence", gt_contact=0, gt_route=None),

    # Fable — first renewal, needs proforma (process block)
    O(id="INV-42", client="fable", amount=65000, issue_ago=36,
      inv_number="INV-2026-041",  # duplicate number, shared with Nexora INV-15
      reply="AP needs a proforma before they can process.",
      gt_dx="process_block", gt_action="fix_and_resubmit", gt_contact=0, gt_route="rhea"),

    # Kestrel — stalling (lapsed) + approver bottleneck (no extension)
    O(id="INV-45", client="kestrel", amount=140000, issue_ago=93,
      gt_dx="stalling", gt_action="escalate", gt_contact=0, gt_route="aditya"),
    O(id="INV-46", client="kestrel", amount=125000, issue_ago=50,
      reply="We're waiting on our own client to pay us first.",
      gt_dx="approver_bottleneck", gt_action="request_delegate_no_extension", gt_contact=1, gt_route="meera"),

    # Amrit — dead contact
    O(id="INV-49", client="amrit", amount=190000, issue_ago=118, contact_verified=0,
      reply="Delivery failed: recipient mailbox unavailable.",
      gt_dx="wrong_contact", gt_action="ask_once_then_escalate_flag_contact", gt_contact=1, gt_route="aditya"),

    # Lumen — failed auto-debit
    O(id="INV-52", client="lumen", amount=120000, issue_ago=20,
      reply="Auto-debit failed - card on file expired.",
      gt_dx="process_block", gt_action="send_reauth_link", gt_contact=1, gt_route="meera"),

    # Vantage — approver bottleneck (strategic) + late-fee waiver earned
    O(id="INV-55", client="vantage", amount=240000, issue_ago=24,
      reply="Awaiting internal sign-off.",
      gt_dx="approver_bottleneck", gt_action="warm_nudge_cap_two", gt_contact=1, gt_route="snehal"),
    O(id="INV-56", client="vantage", amount=240000, issue_ago=8,
      reply="Heads-up: this month's payment may run a few days late.",
      gt_dx="not_actually_late", gt_action="waive_late_fee", gt_contact=0, gt_route="snehal"),

    # Saffron — stalling (firm register)
    O(id="INV-58", client="saffron", amount=150000, issue_ago=50,
      gt_dx="stalling", gt_action="escalate_at_contact_two", gt_contact=1, gt_route="snehal"),

    # Verma — cash stress (red, no plan) + hostile reply
    O(id="INV-60", client="verma", amount=220000, issue_ago=94,
      reply="Cash is very tight right now.",
      gt_dx="cash_stress", gt_action="escalate_no_plan", gt_contact=0, gt_route="aditya"),
    O(id="INV-61", client="verma", amount=210000, issue_ago=40,
      reply="This is absurd - stop chasing us.",
      gt_dx="stalling", gt_action="stop_route_founder", gt_contact=0, gt_route="aditya"),
]


def build_rows():
    """Assemble invoice rows and roll up per-client payment metrics."""
    metrics = {c["id"]: dict(paid=0, late=0, dtp_sum=0, worst_late=0) for c in CLIENTS}
    invoice_rows = []
    auto_num = 100  # sequence for external invoice numbers

    # Historical paid invoices
    for p in PAID:
        c = CLIENT_BY_ID[p["client"]]
        terms = c["terms"]
        issue = REF - timedelta(days=p["issue_ago"])
        due = issue + timedelta(days=terms)
        paid = issue + timedelta(days=p["dtp"])
        days_late = p["dtp"] - terms  # negative if paid early

        m = metrics[p["client"]]
        m["paid"] += 1
        m["dtp_sum"] += p["dtp"]
        if days_late > 0:
            m["late"] += 1
            m["worst_late"] = max(m["worst_late"], days_late)

        auto_num += 1
        invoice_rows.append(dict(
            invoice_id=p["id"], invoice_number=f"INV-2026-{auto_num:03d}",
            client_id=p["client"], revenue_line=c["line"],
            amount_inr=p["amount"], currency="INR",
            issue_date=iso(issue), terms_days=terms, due_date=iso(due),
            status="paid", paid_date=iso(paid), amount_paid_inr=p["amount"],
            days_past_terms=(paid - due).days,
            po_number=None, po_matched=None, milestone_ref=None,
            contact_verified=1, reply_text=None,
            promise_date=None, promise_status=None,
            routed_to=None,
            gt_diagnosis=None, gt_correct_action=None,
            gt_should_contact=0, gt_route_to=None,
        ))

    # Open / actionable invoices
    for o in OPEN:
        c = CLIENT_BY_ID[o["client"]]
        terms = c["terms"]
        issue = REF - timedelta(days=o["issue_ago"])
        due = issue + timedelta(days=terms)
        auto_num += 1
        invoice_rows.append(dict(
            invoice_id=o["id"],
            invoice_number=o["inv_number"] or f"INV-2026-{auto_num:03d}",
            client_id=o["client"], revenue_line=c["line"],
            amount_inr=o["amount"], currency="INR",
            issue_date=iso(issue), terms_days=terms, due_date=iso(due),
            status=o["status"], paid_date=None, amount_paid_inr=o["amount_paid"],
            days_past_terms=(REF - due).days,
            po_number=o["po"], po_matched=o["po_matched"],
            milestone_ref=o["milestone"], contact_verified=o["contact_verified"],
            reply_text=o["reply"], promise_date=o["promise_date"],
            promise_status=o["promise_status"],
            routed_to=c["owner"],
            gt_diagnosis=o["gt_dx"], gt_correct_action=o["gt_action"],
            gt_should_contact=o["gt_contact"], gt_route_to=o["gt_route"],
        ))

    # Roll metrics into client rows and compute tier
    client_rows = []
    for c in CLIENTS:
        m = metrics[c["id"]]
        avg_dtp = round(m["dtp_sum"] / m["paid"], 1) if m["paid"] else None
        tier = compute_tier(
            invoices_paid=m["paid"],
            promises_broken=c["pb"],
            worst_days_late=m["worst_late"],
            response_rate=c["resp"],
        )
        client_rows.append(dict(
            client_id=c["id"], name=c["name"], industry=c["industry"],
            revenue_line=c["line"], segment=c["segment"],
            payment_terms_days=c["terms"], internal_owner_id=c["owner"],
            ap_contact_name=c["ap_name"], ap_contact_verified=c["ap_verified"],
            client_finance_portal=c["portal"], relationship_years=c["years"],
            total_revenue_inr=c["revenue"],
            referred_by=REFERRED_BY.get(c["id"]),
            referrals_made=REFERRALS_MADE.get(c["id"], 0),
            invoices_paid=m["paid"], invoices_paid_late=m["late"],
            avg_days_to_pay=avg_dtp, worst_days_late=m["worst_late"],
            response_rate=c["resp"], engages_off_topic=c["off_topic"],
            promises_made=c["pm"], promises_kept=c["pk"], promises_broken=c["pb"],
            tier=tier,
        ))

    return client_rows, invoice_rows


def insert_dicts(conn, table, rows):
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [[r[c] for c in cols] for r in rows])


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema_sql = f.read()

    client_rows, invoice_rows = build_rows()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(schema_sql)
        conn.executemany(
            "INSERT INTO team (person_id, name, role, service_line, "
            "handles_escalation_types) VALUES (?, ?, ?, ?, ?)", TEAM)
        insert_dicts(conn, "clients", client_rows)
        insert_dicts(conn, "invoices", invoice_rows)
        conn.commit()
    finally:
        conn.close()

    tier_counts = {}
    for r in client_rows:
        tier_counts[r["tier"]] = tier_counts.get(r["tier"], 0) + 1

    print(f"Built {DB_PATH}")
    print(f"  team:     {len(TEAM)}")
    print(f"  clients:  {len(client_rows)}  "
          f"(green {tier_counts.get('green',0)}, "
          f"amber {tier_counts.get('amber',0)}, "
          f"red {tier_counts.get('red',0)})")
    print(f"  invoices: {len(invoice_rows)}  "
          f"({len(PAID)} paid history + {len(OPEN)} open/actionable)")


if __name__ == "__main__":
    main()
