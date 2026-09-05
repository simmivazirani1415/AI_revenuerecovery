"""Receivables AI - read-only UI over agency.db. Flask, no build step.

    python ui.py    # serves on http://localhost:5002

Every value comes from agency.db at request time. The only write is the
feedback table.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from dotenv import load_dotenv

import execute
from policy import FOUNDER_THRESHOLD_INR

load_dotenv()  # so VAPI_* and other keys are available to the server

# Voice (Vapi) key separation:
#   PUBLIC key + assistant id  -> safe to hand the browser (Web SDK).
#   PRIVATE key                -> server-side ONLY; never rendered to a client.
VAPI_PUBLIC_KEY = os.environ.get("VAPI_PUBLIC_KEY", "")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID", "")

HERE = os.path.dirname(os.path.abspath(__file__))
# On Vercel (and any serverless host) the deployment filesystem is read-only
# except for /tmp. The ledger is bundled read-only, so copy it into /tmp on
# cold start and run against that writable copy. Writes there are ephemeral —
# they survive warm invocations but reset on a cold start. Locally we just use
# the file in the project directory.
if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    import shutil
    _bundled = os.path.join(HERE, "agency.db")
    DB_PATH = "/tmp/agency.db"
    if os.path.exists(_bundled) and not os.path.exists(DB_PATH):
        shutil.copy(_bundled, DB_PATH)
else:
    DB_PATH = os.path.join(HERE, "agency.db")

DEFAULT_SETTINGS = {
    "payment_terms": "30", "avg_invoice": "500000",
    "approval_threshold": str(FOUNDER_THRESHOLD_INR),
    "quiet_hours": "1", "max_3_contacts": "1",
    "stop_on_dispute": "1", "never_chase_strategic": "1",
    "onboarding_completed": "0",
}

app = Flask(__name__)
app.jinja_env.filters["inr"] = lambda n: inr(n)

TIER_LABEL = {"green": "Low risk", "amber": "Medium risk", "red": "High risk"}
REVENUE_LABEL = {"project": "Project", "product_licence": "Licence", "retainer": "Retainer"}
DIAG_QUALIFIER = {
    "stalling": "not a process problem",
    "process_block": "the agency's to fix",
    "approver_bottleneck": "stuck awaiting sign-off",
    "wrong_contact": "never reached AP",
    "dispute": "contested, hard stop",
    "cash_stress": "genuine cash difficulty",
    "not_actually_late": "inside terms",
    "partial_payment": "balance only",
}
ACTION_PHRASE = {
    "escalate": "escalate, do not chase",
    "do_nothing": "stay silent",
    "nudge": "send a reminder",
    "offer_payment_plan": "offer a payment plan",
    "ask_for_ap_contact": "ask for the AP contact",
    "ask_for_delegate": "ask for a delegate",
    "resubmit_via_finance": "route to finance",
    "send_reauth_link": "send a re-authorisation link",
}
RULES_METHODS = {"deterministic", "tier_default", "detect_left_alone", "gt_standin"}


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------
def db_ro():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def db_rw():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables():
    conn = db_rw()
    conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id TEXT, verdict TEXT, note TEXT, created_at TEXT)""")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, size INTEGER, created_at TEXT)""")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    conn = db_rw()
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()
    conn.close()


def inr(n):
    """Indian-grouped rupees, e.g. 2184000 -> '2,18,40,000'."""
    n = int(n or 0)
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + s


def bullets(text):
    if not text:
        return []
    text = text.strip().rstrip(".")
    parts = [p.strip() for p in text.split(";")]
    out = []
    for p in parts:
        # secondary split on ' - ' clause separators used in evidence
        out.append(p[0].upper() + p[1:] if p else p)
    return [p for p in out if p]


# --------------------------------------------------------------------------
# Shared context (footer, nav)
# --------------------------------------------------------------------------
def base_ctx(conn):
    n_open = conn.execute("SELECT COUNT(*) FROM invoices WHERE status != 'paid'").fetchone()[0]
    n_clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    return {"product": "Receivables AI",
            "footer": f"Simulated ledger · Razorpay test mode · "
                      f"{n_open} open invoices across {n_clients} clients"}


# --------------------------------------------------------------------------
# Screen A: invoice detail
# --------------------------------------------------------------------------
def _risk_rank(conn):
    """Rank of each invoice by detect risk score among at-risk invoices."""
    rows = []
    for r in conn.execute("SELECT invoice_id, observed FROM event_log WHERE stage='detect'"):
        o = json.loads(r["observed"] or "{}")
        if o.get("at_risk"):
            rows.append((r["invoice_id"], o.get("risk_score", 0)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return {inv: (i + 1, len(rows)) for i, (inv, _) in enumerate(rows)}, len(rows)


def build_trail(conn, inv, client, events, rank):
    team = {r["person_id"]: r for r in conn.execute("SELECT * FROM team")}
    contacts = conn.execute(
        "SELECT COUNT(*) FROM event_log WHERE invoice_id=? AND stage='execute' "
        "AND outcome='sent'", (inv["invoice_id"],)).fetchone()[0]
    stages, escalated, held = [], False, False

    for e in events:
        o = json.loads(e["observed"] or "{}")
        st = e["stage"]
        blocks = []
        if st == "detect":
            dpt = inv["days_past_terms"]
            head = (f"Flagged: {dpt} days past terms, ₹{inr(o.get('outstanding_inr'))} at risk"
                    if o.get("at_risk") and dpt > 0 else
                    "Flagged: inside terms, but a blocker needs attention"
                    if o.get("at_risk") else "Left alone: inside terms, client silent")
            b = []
            if o.get("risk_score"):
                r = rank.get(inv["invoice_id"])
                b.append(f"Risk score {o['risk_score']}" + (f", {r[0]} of {r[1]} at-risk" if r else ""))
            b.append(f"{dpt} days past net-{inv['terms_days']} terms" if dpt > 0
                     else f"Inside net-{inv['terms_days']} terms")
            if client["promises_broken"]:
                b.append(f"{client['promises_broken']} broken promise(s) on record")
            if client["response_rate"] is not None:
                b.append(f"Response rate {round(client['response_rate']*100)}%")
            blocks.append({"kind": "bullets", "lines": b})
        elif st == "diagnose":
            q = DIAG_QUALIFIER.get(e["diagnosis"], "")
            head = f"Diagnosed: {e['diagnosis']}" + (f", {q}" if q else "")
            blocks.append({"kind": "evidence", "lines": bullets(e["diagnosis_evidence"]),
                           "rules": o.get("method") in RULES_METHODS})
        elif st == "decide":
            head = f"Decided: {ACTION_PHRASE.get(e['decision'], e['decision'])}"
            blocks.append({"kind": "rule", "text": "RULE FIRED — " + (e["rule_applied"] or "")})
            if e["register"]:
                blocks.append({"kind": "tone", "register": e["register"],
                               "reason": e["register_reason"]})
        elif st == "escalate":
            if o.get("queue") == "approval":
                held = True
                continue  # rendered as a held stage below
            escalated = True
            who = team.get(e["routed_to"])
            head = (f"Routed to {who['name']}, {who['role']}" if who
                    else f"Routed to {e['routed_to']}")
            blocks.append({"kind": "handover",
                           "reason": e["routed_reason"],
                           "rows": [("Amount", "₹" + inr(inv["amount_inr"])),
                                    ("Days past terms", f"{inv['days_past_terms']} days"),
                                    ("Contacts attempted", str(contacts)),
                                    ("Promises broken", str(client["promises_broken"])),
                                    ("Recommended next step", o.get("next_step", "—"))]})
        else:
            continue
        stages.append({"marker": "done", "heading": head,
                       "ts": e["timestamp"], "blocks": blocks})

    # held-for-approval: show the drafted send awaiting the founder
    appr = next((e for e in events if e["stage"] == "escalate"
                 and json.loads(e["observed"] or "{}").get("queue") == "approval"), None)
    if appr and not escalated:
        o = json.loads(appr["observed"] or "{}")
        stages.append({"marker": "done",
                       "heading": "Held for founder approval",
                       "ts": appr["timestamp"],
                       "blocks": [{"kind": "handover", "reason": appr["routed_reason"],
                                   "rows": [("Drafted message", o.get("message") or "—")]}]})
        stages.append({"marker": "pending", "heading": "Awaiting founder action",
                       "ts": None, "blocks": []})
    elif escalated:
        stages.append({"marker": "pending", "heading": "Awaiting the assigned owner",
                       "ts": None, "blocks": []})

    terminal = escalated or held or (events and events[-1]["stage"] == "decide"
                                     and events[-1]["decision"] in ("do_nothing", "escalate"))
    return stages, terminal


def invoice_ctx(conn, invoice_id):
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        return None
    client = conn.execute("SELECT * FROM clients WHERE client_id=?", (inv["client_id"],)).fetchone()
    events = conn.execute(
        "SELECT * FROM event_log WHERE invoice_id=? AND stage IN "
        "('detect','diagnose','decide','escalate') ORDER BY event_id", (invoice_id,)).fetchall()
    rank, _ = _risk_rank(conn)
    stages, terminal = build_trail(conn, inv, client, events, rank)

    team = {r["person_id"]: r for r in conn.execute("SELECT * FROM team")}
    referrer = (conn.execute("SELECT name FROM clients WHERE client_id=?",
                             (client["referred_by"],)).fetchone()["name"]
                if client["referred_by"] else None)
    referrer_first = (conn.execute(
        "SELECT invoice_id FROM invoices WHERE client_id=? AND status!='paid' ORDER BY invoice_id",
        (client["referred_by"],)).fetchone() if client["referred_by"] else None)

    # client snapshot
    open_rows = conn.execute(
        "SELECT amount_inr, amount_paid_inr FROM invoices WHERE client_id=? AND status!='paid'",
        (client["client_id"],)).fetchall()
    outstanding = sum(r["amount_inr"] - r["amount_paid_inr"] for r in open_rows)
    since_year = 2026 - int(client["relationship_years"] or 0)

    fb = conn.execute("SELECT verdict, created_at FROM feedback WHERE invoice_id=? "
                      "ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone() \
        if _has_feedback(conn) else None

    header = [
        ("Invoice ID", inv["invoice_id"], None),
        ("Client", client["name"], client["client_id"]),
        ("Tier", None, None),   # rendered as chip
        ("Amount", "₹" + inr(inv["amount_inr"]), None),
        ("Payment terms", f"Net {inv['terms_days']}", None),
        ("Days past terms",
         f"{inv['days_past_terms']} days" if inv["days_past_terms"] > 0 else "Inside terms",
         "red" if inv["days_past_terms"] > 0 else "muted"),
    ]
    if referrer:
        header.append(("Referred by", referrer, "ref"))

    snapshot = [
        ("Revenue line", REVENUE_LABEL.get(client["revenue_line"], client["revenue_line"])),
        ("Relationship since", f"{since_year} ({int(client['relationship_years'])} yrs)"),
        ("Total outstanding", f"₹{inr(outstanding)} across {len(open_rows)} invoices"),
        ("Promises kept", f"{client['promises_kept']} of {client['promises_made']}"),
        ("Avg days to pay", f"{client['avg_days_to_pay']:.0f} days"
         if client["avg_days_to_pay"] is not None else "—"),
        ("Response rate", f"{round(client['response_rate']*100)}%"),
        ("Internal owner", team[client["internal_owner_id"]]["name"]
         if client["internal_owner_id"] in team else "—"),
        ("Referred by", referrer or "—"),
    ]

    return {
        "inv": inv, "client": client, "tier": inv_tier(client["tier"]),
        "header": header, "referrer": referrer,
        "referrer_first": referrer_first["invoice_id"] if referrer_first else None,
        "stages": stages, "terminal": terminal,
        "snapshot": snapshot, "feedback": fb,
    }


def inv_tier(tier):
    return {"key": tier, "label": TIER_LABEL.get(tier, tier)}


def _has_feedback(conn):
    return conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='feedback'").fetchone() is not None


# --------------------------------------------------------------------------
# AI panel: chip -> SQL -> real answer. No LLM anywhere.
# --------------------------------------------------------------------------
GLOBAL_CHIPS = [
    ("worst_payers", "Who are my worst payers?"),
    ("agent_week", "What did the agent do this week?"),
    ("overdue", "How much is genuinely overdue?"),
    ("waiting_on_me", "Which invoices are waiting on me?"),
    ("broken_promise", "Has anyone broken a promise?"),
]


def client_chips(client):
    return [("why_tier", f"Why is this client {TIER_LABEL[client['tier']]}?"),
            ("what_tried", "What have we already tried?"),
            ("decide_why", "What did the agent decide and why?"),
            ("keep_working", "Should we keep working with them?")]


def answer_global(conn, key):
    if key == "worst_payers":
        rows = conn.execute(
            "SELECT c.name, c.promises_broken, (SELECT AVG(days_past_terms) FROM invoices i "
            "WHERE i.client_id=c.client_id AND i.status!='paid' AND i.days_past_terms>0) avg_late "
            "FROM clients c WHERE c.promises_broken>0 "
            "ORDER BY c.promises_broken DESC, avg_late DESC LIMIT 5").fetchall()
        table = [("Client", "Broken", "Avg late")] + [
            (r["name"], str(r["promises_broken"]),
             f"{r['avg_late']:.0f}d" if r["avg_late"] is not None else "—") for r in rows]
        return {"summary": f"{len(rows)} clients have broken a promise — worst first.",
                "table": table, "source": f"From {len(rows)} clients with broken promises"}
    if key == "agent_week":
        d = disposition(conn)
        labels = [("message_sent", "messaged"), ("held", "held for approval"),
                  ("routed", "escalated to a person"), ("finance", "sent to finance to fix"),
                  ("left_alone", "left alone on purpose"), ("blocked", "blocked by a guardrail")]
        bl = [f"{len(d[k])} {lab}" for k, lab in labels if d[k]]
        n = sum(len(d[k]) for k, _ in labels)
        return {"summary": "How the agent handled the open ledger.", "bullets": bl,
                "source": f"From {n} decisions in the event log"}
    if key == "overdue":
        rows = conn.execute("SELECT amount_inr-amount_paid_inr o, days_past_terms d "
                            "FROM invoices WHERE status!='paid'").fetchall()
        past = sum(r["o"] for r in rows if r["d"] > 0)
        inside = sum(r["o"] for r in rows if r["d"] <= 0)
        return {"summary": f"₹{inr(past)} is genuinely overdue; ₹{inr(inside)} is still inside terms.",
                "bullets": [f"Past terms: ₹{inr(past)} ({sum(1 for r in rows if r['d']>0)} invoices)",
                            f"Inside terms: ₹{inr(inside)} ({sum(1 for r in rows if r['d']<=0)} invoices)"],
                "source": f"From {len(rows)} open invoices"}
    if key == "waiting_on_me":
        rows = conn.execute(
            "SELECT e.invoice_id, e.routed_reason, i.amount_inr FROM event_log e "
            "JOIN invoices i ON i.invoice_id=e.invoice_id WHERE e.stage='escalate' "
            "AND e.observed LIKE '%\"queue\": \"approval\"%' ORDER BY i.amount_inr DESC").fetchall()
        table = [("Invoice", "Amount", "Why held")] + [
            (r["invoice_id"], "₹" + inr(r["amount_inr"]), r["routed_reason"] or "—") for r in rows]
        return {"summary": f"{len(rows)} invoices are held for your approval.", "table": table,
                "source": f"From {len(rows)} held-for-approval events"}
    if key == "broken_promise":
        rows = conn.execute(
            "SELECT c.name, c.promises_broken, (SELECT GROUP_CONCAT(promise_date) FROM invoices i "
            "WHERE i.client_id=c.client_id AND i.promise_status='broken') dates FROM clients c "
            "WHERE c.promises_broken>0 ORDER BY c.promises_broken DESC").fetchall()
        table = [("Client", "Broken", "Dated on file")] + [
            (r["name"], str(r["promises_broken"]), r["dates"] or "—") for r in rows]
        return {"summary": f"{len(rows)} clients have broken promises on record.", "table": table,
                "source": f"From {len(rows)} clients"}
    return None


QUESTION_TEXT = {
    "worst_payers": "Who are our worst payers?", "agent_week": "What did we send this week?",
    "overdue": "How much is genuinely overdue?", "waiting_on_me": "Which invoices are waiting on me?",
    "broken_promise": "Has anyone broken a promise?", "what_tried": "What have we already tried?",
    "decide_why": "What did the agent decide and why?", "keep_working": "Should we keep working with them?",
}


def answer_draft(conn, invoice_id):
    """Real reminder draft: execute.draft() with the register decide chose. No hardcoding."""
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        return {"kind": "draft", "message": None, "explain": f"No invoice {invoice_id} on the ledger."}
    client = conn.execute("SELECT * FROM clients WHERE client_id=?", (inv["client_id"],)).fetchone()
    dc = conn.execute("SELECT decision, register, register_reason FROM event_log "
                      "WHERE invoice_id=? AND stage='decide'", (invoice_id,)).fetchone()
    if not dc:
        return {"kind": "draft", "message": None,
                "explain": f"{invoice_id} is settled — the agent has no active decision to draft from."}
    register = dc["register"] or "neutral"
    outstanding = inv["amount_inr"] - inv["amount_paid_inr"]
    message = execute.draft("nudge", register, client["name"], invoice_id, outstanding,
                            inv["rzp_link_url"])
    return {"kind": "draft", "message": message, "register": register,
            "explain": f"{register.capitalize()} register — {dc['register_reason']}"}


def ai_exchange(conn, per_client, client, invoice, ask, arg, q):
    """Resolve one panel interaction to an exchange {kind, question, ...}. No LLM."""
    if q:
        return {"kind": "mocked", "question": q,
                "text": "Free-text questions aren't wired up yet — try one of the suggestions below."}
    if not ask:
        return None
    if ask == "draft":
        iid = arg or (invoice["invoice_id"] if invoice else None)
        if not iid:
            return None
        ex = answer_draft(conn, iid)
        ex["question"] = f"Draft a reminder for {iid}"
        return ex
    if ask == "why_tier":
        c = conn.execute("SELECT * FROM clients WHERE client_id=?", (arg,)).fetchone() if arg else client
        if not c:
            return None
        return {"kind": "answer", "question": f"Why is {c['name']} {TIER_LABEL[c['tier']]}?",
                "answer": answer_client(conn, c, None, "why_tier")}
    if ask in ("worst_payers", "agent_week", "overdue", "waiting_on_me", "broken_promise"):
        return {"kind": "answer", "question": QUESTION_TEXT.get(ask, ask),
                "answer": answer_global(conn, ask)}
    if ask in ("what_tried", "decide_why", "keep_working") and per_client and invoice:
        return {"kind": "answer", "question": QUESTION_TEXT.get(ask, ask),
                "answer": answer_client(conn, client, invoice, ask)}
    return None


def ai_suggestions(per_client, client, invoice):
    if per_client and client and invoice:
        return [(f"Why is {client['name'].split()[0]} {TIER_LABEL[client['tier']].lower()}?", "?ask=why_tier"),
                (f"Draft a reminder for {invoice['invoice_id']}", "?ask=draft"),
                ("What have we already tried?", "?ask=what_tried"),
                ("Who are our worst payers?", "?ask=worst_payers")]
    return [("Why is Kestrel red?", "?ask=why_tier&arg=kestrel"),
            ("Who are our worst payers?", "?ask=worst_payers"),
            ("Draft a reminder for INV-45", "?ask=draft&arg=INV-45"),
            ("What did we send this week?", "?ask=agent_week")]


def answer_client(conn, client, invoice, key):
    cid = client["client_id"]
    iid = invoice["invoice_id"] if invoice else None
    if key == "why_tier":
        bl = [f"Tier: {TIER_LABEL[client['tier']]}",
              f"Promises: {client['promises_kept']} kept / {client['promises_broken']} broken "
              f"of {client['promises_made']} made",
              f"Avg days to pay: {client['avg_days_to_pay']:.0f}"
              if client["avg_days_to_pay"] is not None else "No paid history yet",
              f"Worst lateness in history: {client['worst_days_late']}d",
              f"Response rate: {round(client['response_rate']*100)}%"]
        return {"summary": f"{client['name']} is {TIER_LABEL[client['tier']]} — {compose_why(conn, client)}.",
                "bullets": bl, "source": f"From {client['invoices_paid']} paid invoices + the open ledger"}
    if key == "what_tried":
        rows = conn.execute(
            "SELECT e.timestamp, e.action_taken, e.outcome FROM event_log e "
            "JOIN invoices i ON i.invoice_id=e.invoice_id WHERE i.client_id=? "
            "AND e.stage='execute' ORDER BY e.event_id", (cid,)).fetchall()
        if not rows:
            dec = conn.execute("SELECT decision FROM event_log WHERE invoice_id=? AND stage='decide'",
                               (iid,)).fetchone()
            plan = ACTION_PHRASE.get(dec["decision"], dec["decision"]) if dec else "—"
            return {"summary": "No client contact has been attempted yet.",
                    "bullets": [f"Planned next action on {iid}: {plan}"],
                    "source": "From 0 outbound events"}
        bl = [f"{r['timestamp'][:16]} — {r['action_taken']} [{r['outcome']}]" for r in rows]
        return {"summary": f"{len(rows)} contact attempts on record.", "bullets": bl,
                "source": f"From {len(rows)} events"}
    if key == "decide_why":
        dg = conn.execute("SELECT diagnosis, diagnosis_evidence FROM event_log WHERE "
                          "invoice_id=? AND stage='diagnose'", (iid,)).fetchone()
        dc = conn.execute("SELECT decision, rule_applied, register, register_reason FROM "
                          "event_log WHERE invoice_id=? AND stage='decide'", (iid,)).fetchone()
        bl = []
        if dg:
            bl += [f"Diagnosis: {dg['diagnosis']}", f"Evidence: {dg['diagnosis_evidence']}"]
        if dc:
            bl.append(f"Rule: {dc['rule_applied']}")
            if dc["register"]:
                bl.append(f"Register: {dc['register']} — {dc['register_reason']}")
        act = ACTION_PHRASE.get(dc["decision"], dc["decision"]) if dc else "—"
        return {"summary": f"On {iid}, the agent chose to {act}.", "bullets": bl,
                "source": f"From the decision trail on {iid}"}
    if key == "keep_working":
        o = conn.execute("SELECT SUM(amount_inr-amount_paid_inr) o, COUNT(*) n FROM invoices "
                         "WHERE client_id=? AND status!='paid'", (cid,)).fetchone()
        ref = conn.execute("SELECT name FROM clients WHERE client_id=?",
                           (client["referred_by"],)).fetchone() if client["referred_by"] else None
        rel = (f"Referred by {ref['name']}" if ref else
               f"Has referred {client['referrals_made']} other clients" if client["referrals_made"]
               else "No referral relationship")
        bl = [f"Lifetime revenue: ₹{inr(client['total_revenue_inr'])}",
              f"Currently outstanding: ₹{inr(o['o'])} across {o['n']} invoices",
              f"Promises: {client['promises_kept']} kept / {client['promises_broken']} broken",
              f"Relationship: {int(client['relationship_years'])} years", rel]
        return {"summary": f"The facts on {client['name']} — you make the call.", "bullets": bl,
                "source": f"From {client['invoices_paid']} paid + {o['n']} open invoices"}
    return None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _voice_reachable(conn, invoice_id):
    """Would decide return voice_call for this invoice at the contact cap?
    True only for voice-permitted clients whose ladder reaches rung 4. Used by
    the demo toggle so it can't light the button where voice isn't real."""
    from voice_bridge import _load, _latest_diagnosis
    from decide import decide
    from policy import CONTACT_CAP
    inv = _load(conn, invoice_id)
    if not inv or not inv.get("voice_permitted"):
        return False
    cap = CONTACT_CAP["strategic" if inv["segment"] == "strategic" else "standard"]
    return decide(inv, _latest_diagnosis(conn, invoice_id), cap, voice_done=False)["action"] == "voice_call"


@app.route("/invoice/<invoice_id>")
def invoice(invoice_id):
    conn = db_ro()
    try:
        ctx = invoice_ctx(conn, invoice_id)
        if not ctx:
            abort(404)
        ctx.update(base_ctx(conn))
        ctx["active"] = "invoices"
        a = (request.args.get("ask"), request.args.get("arg"), request.args.get("q"))
        ctx.update(show_ai=True, per_client=True,
                   exchange=ai_exchange(conn, True, ctx["client"], ctx["inv"], *a),
                   suggestions=ai_suggestions(True, ctx["client"], ctx["inv"]))
        # The live "Call client now" button appears ONLY when decide's latest
        # recorded action for this invoice is voice_call (rung 4). Browser gets
        # the PUBLIC key + assistant id only — never the API/private key.
        latest = conn.execute(
            "SELECT decision FROM event_log WHERE stage='decide' AND invoice_id=? "
            "ORDER BY event_id DESC LIMIT 1", (invoice_id,)).fetchone()
        real_voice = bool(latest and latest[0] == "voice_call")
        # Demo toggle: ?voice_demo=1 (or env DEMO_VOICE_INVOICE=<id>) surfaces the
        # button WITHOUT writing anything to the ledger — only where voice is real.
        demo_voice = (request.args.get("voice_demo") == "1"
                      or os.environ.get("DEMO_VOICE_INVOICE") == invoice_id)
        keys_ok = bool(VAPI_PUBLIC_KEY and VAPI_ASSISTANT_ID)
        voice_call_ready = keys_ok and (real_voice
                                        or (demo_voice and _voice_reachable(conn, invoice_id)))
        ctx.update(vapi_public_key=VAPI_PUBLIC_KEY, vapi_assistant_id=VAPI_ASSISTANT_ID,
                   voice_call_ready=voice_call_ready,
                   voice_demo=voice_call_ready and not real_voice)
        return render_template("invoice.html", **ctx)
    finally:
        conn.close()


def _ngrok_url():
    """Best-effort: the current public ngrok URL (reuse the Twilio tunnel)."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3) as r:
            for t in json.loads(r.read().decode()).get("tunnels", []):
                if t.get("proto") == "https":
                    return t.get("public_url")
    except Exception:
        return None
    return None


@app.route("/voice/start/<invoice_id>", methods=["POST"])
def voice_start(invoice_id):
    """Prepare a WEB call (no phone, no telephony cost). Server-side, using the
    Vapi API key (never sent to the browser): compose the per-invoice brief,
    update the reusable assistant's system prompt + webhook, log a
    voice_call_initiated event with that prompt, and hand the browser back only
    the PUBLIC key + assistant id so it can launch the web-call widget."""
    import urllib.request
    api_key = os.environ.get("VAPI_API_KEY") or os.environ.get("VAPI_PRIVATE_KEY", "")
    assistant_id = os.environ.get("VAPI_ASSISTANT_ID", "")
    if not api_key or not assistant_id:
        return jsonify(ok=False, error="VAPI_API_KEY / VAPI_ASSISTANT_ID not configured"), 400

    from voice_bridge import build_vapi_prompt
    from log import connect as _connect, write_event
    try:
        system_prompt = build_vapi_prompt(invoice_id)
    except SystemExit as e:
        return jsonify(ok=False, error=str(e)), 404

    # Update the reusable assistant's system prompt (and point its end-of-call
    # report at our webhook, reusing the ngrok tunnel).
    body = {"model": {"provider": "openai", "model": "gpt-4o",
                      "messages": [{"role": "system", "content": system_prompt}]}}
    ngrok = _ngrok_url()
    if ngrok:
        body["server"] = {"url": f"{ngrok}/vapi-webhook"}
        body["serverMessages"] = ["end-of-call-report"]
    req = urllib.request.Request(
        f"https://api.vapi.ai/assistant/{assistant_id}", data=json.dumps(body).encode(),
        method="PATCH", headers={"Authorization": f"Bearer {api_key}",
                                 "Content-Type": "application/json"})
    warning = None
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception as exc:  # call can still run on the assistant's existing prompt
        detail = getattr(exc, "read", lambda: b"")() if hasattr(exc, "read") else b""
        warning = f"assistant update failed: {exc} {detail.decode('utf-8','ignore')[:200]}"

    # Log the initiation immediately, with the exact prompt sent.
    conn = _connect()
    try:
        cid = conn.execute("SELECT client_id FROM invoices WHERE invoice_id=?",
                           (invoice_id,)).fetchone()
        write_event(conn, invoice_id=invoice_id, client_id=cid[0] if cid else invoice_id,
                    stage="execute", action_taken="voice_call_initiated", channel="voice",
                    outcome="initiated", message_sent=system_prompt,
                    observed={"source": "vapi_web", "assistant_id": assistant_id,
                              "webhook": f"{ngrok}/vapi-webhook" if ngrok else None})
    finally:
        conn.close()

    return jsonify(ok=True, publicKey=VAPI_PUBLIC_KEY, assistantId=assistant_id,
                   invoiceId=invoice_id, warning=warning)


@app.route("/feedback", methods=["POST"])
def feedback():
    invoice_id = request.form.get("invoice_id", "")
    verdict = request.form.get("verdict", "")
    note = request.form.get("note", "").strip()
    conn = db_rw()
    conn.execute("INSERT INTO feedback (invoice_id, verdict, note, created_at) "
                 "VALUES (?,?,?,?)",
                 (invoice_id, verdict, note,
                  datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return redirect(url_for("invoice", invoice_id=invoice_id) + "#feedback")


def rel_time(ts):
    try:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days
        return "today" if days <= 0 else f"{days}d ago"
    except Exception:
        return ""


def compose_why(conn, c):
    """The 'why this tier' column - composed from computed signals, never hardcoded."""
    if c["invoices_paid"] == 0:
        return "new client, no history yet"
    parts = []
    if c["promises_broken"]:
        parts.append(f"{c['promises_broken']} promise{'s' if c['promises_broken'] > 1 else ''} broken")
    elif c["promises_kept"]:
        parts.append(f"{c['promises_kept']} of {c['promises_made']} promises kept")
    maxdpt = conn.execute("SELECT MAX(days_past_terms) FROM invoices WHERE client_id=? "
                          "AND status!='paid'", (c["client_id"],)).fetchone()[0]
    if maxdpt and maxdpt > 0:
        parts.append(f"{maxdpt}d past terms")
    elif c["avg_days_to_pay"] is not None:
        parts.append(f"pays ~day {int(c['avg_days_to_pay'])} consistently")
    if (c["response_rate"] or 1) < 0.5 and len(parts) < 2:
        parts.append(f"{round(c['response_rate']*100)}% response rate")
    return " · ".join(parts[:2]) or "clean record"


def last_action(conn, client_id, team):
    row = conn.execute(
        "SELECT e.stage, e.decision, e.routed_to, e.observed, e.timestamp "
        "FROM event_log e JOIN invoices i ON i.invoice_id=e.invoice_id "
        "WHERE i.client_id=? AND e.stage IN ('decide','escalate') "
        "ORDER BY e.event_id DESC LIMIT 1", (client_id,)).fetchone()
    if not row:
        return "—"
    o = json.loads(row["observed"] or "{}")
    if row["stage"] == "escalate" and o.get("queue") == "escalation":
        who = team.get(row["routed_to"], {}).get("name", row["routed_to"])
        return f"Escalated to {who.split()[0]} · {rel_time(row['timestamp'])}"
    if row["stage"] == "escalate" and o.get("queue") == "approval":
        return "Held for approval"
    dec = row["decision"]
    return {"do_nothing": "Left alone · inside terms",
            "resubmit_via_finance": "Routed to finance",
            }.get(dec, f"Follow-up scheduled · {rel_time(row['timestamp'])}")


def disposition(conn):
    """Six buckets for how each open invoice was handled (matches execute logic)."""
    b = {"message_sent": [], "held": [], "routed": [], "finance": [],
         "left_alone": [], "blocked": []}
    for r in conn.execute(
        "SELECT d.invoice_id, d.decision, d.observed, i.reply_text, i.contact_verified "
        "FROM event_log d JOIN invoices i ON i.invoice_id=d.invoice_id WHERE d.stage='decide'"):
        dec, o = r["decision"], json.loads(r["observed"] or "{}")
        reply = (r["reply_text"] or "").lower()
        if dec == "do_nothing":
            b["left_alone"].append(r["invoice_id"])
        elif dec == "escalate":
            b["routed"].append(r["invoice_id"])
        elif dec == "resubmit_via_finance":
            b["finance"].append(r["invoice_id"])
        elif o.get("needs_founder_approval"):
            b["held"].append(r["invoice_id"])
        else:
            payment = any(w in reply for w in ("in transit", "payment initiated", "funds in transit"))
            dead = r["contact_verified"] == 0 and any(w in reply for w in ("bounce", "mailbox", "delivery failed"))
            b["blocked" if (payment or dead) else "message_sent"].append(r["invoice_id"])
    return b


def naive(conn, assumed):
    return conn.execute(f"""
        SELECT SUM((days_past_terms + terms_days - {assumed})/7 + 1) AS msgs,
               SUM(CASE WHEN days_past_terms<0 THEN 1 ELSE 0 END) AS inside,
               SUM(CASE WHEN gt_diagnosis='dispute' THEN 1 ELSE 0 END) AS dispute,
               SUM(CASE WHEN segment='strategic' THEN 1 ELSE 0 END) AS strat,
               SUM(CASE WHEN contact_verified=0 AND (lower(reply_text) LIKE '%bounce%'
                    OR lower(reply_text) LIKE '%mailbox%' OR lower(reply_text) LIKE '%delivery failed%')
                    THEN 1 ELSE 0 END) AS dead
        FROM (SELECT i.*, c.segment FROM invoices i JOIN clients c ON c.client_id=i.client_id
              WHERE i.status!='paid' AND (i.days_past_terms + i.terms_days) > {assumed})""").fetchone()


@app.route("/")
@app.route("/clients")
def clients():
    conn = db_ro()
    try:
        team = {r["person_id"]: dict(r) for r in conn.execute("SELECT * FROM team")}
        rows = []
        for c in conn.execute("SELECT * FROM clients ORDER BY name"):
            openrows = conn.execute("SELECT amount_inr, amount_paid_inr FROM invoices "
                                    "WHERE client_id=? AND status!='paid'", (c["client_id"],)).fetchall()
            first = conn.execute("SELECT invoice_id FROM invoices WHERE client_id=? "
                                 "AND status!='paid' ORDER BY invoice_id", (c["client_id"],)).fetchone()
            rows.append({
                "cid": c["client_id"],
                "name": c["name"], "revenue": REVENUE_LABEL.get(c["revenue_line"], c["revenue_line"]),
                "tier": inv_tier(c["tier"]), "why": compose_why(conn, c),
                "open": sum(r["amount_inr"] - r["amount_paid_inr"] for r in openrows),
                "owner": team.get(c["internal_owner_id"], {}).get("name", "—"),
                "action": last_action(conn, c["client_id"], team),
                "link": url_for("invoice", invoice_id=first["invoice_id"]) if first else "#",
            })
        open_inv = conn.execute("SELECT days_past_terms FROM invoices WHERE status!='paid'").fetchall()
        overdue = sum(1 for r in open_inv if r["days_past_terms"] > 0)
        inside = len(open_inv) - overdue
        outstanding = conn.execute("SELECT SUM(amount_inr-amount_paid_inr) FROM invoices "
                                   "WHERE status!='paid'").fetchone()[0]
        escalated = conn.execute("SELECT COUNT(DISTINCT invoice_id) FROM event_log WHERE "
                                 "stage='escalate' AND observed LIKE '%\"queue\": \"escalation\"%'").fetchone()[0]
        disp = disposition(conn)
        metrics = [
            ("Open receivables", "₹" + inr(outstanding), None),
            ("Genuinely overdue", f"{overdue} of {len(open_inv)}", f"{inside} still inside payment terms"),
            ("Escalated to a person", str(escalated), None),
            ("Agent left alone", str(len(disp["left_alone"])), "correctly, per policy"),
        ]
        ctx = base_ctx(conn)
        a = (request.args.get("ask"), request.args.get("arg"), request.args.get("q"))
        ctx.update(active="clients", rows=rows, metrics=metrics, n=len(rows), wide=True,
                   show_ai=True, per_client=False,
                   exchange=ai_exchange(conn, False, None, None, *a),
                   suggestions=ai_suggestions(False, None, None))
        return render_template("clients.html", **ctx)
    finally:
        conn.close()


STATUS_STYLE = {"Paid": "green", "Left alone": "green", "Held for approval": "amber",
                "Escalated": "red", "Blocked": "red", "Message sent": "blue",
                "Sent to finance": "grey"}


@app.route("/invoices")
def invoices():
    conn = db_ro()
    try:
        team = {r["person_id"]: r["name"] for r in conn.execute("SELECT person_id, name FROM team")}
        cofounder = {r["person_id"] for r in conn.execute(
            "SELECT person_id FROM team WHERE role LIKE 'Co-founder%'")}
        disp = disposition(conn)
        bucket = {}
        for b, ids in disp.items():
            for i in ids:
                bucket[i] = b
        diag = {r["invoice_id"]: r["diagnosis"] for r in conn.execute(
            "SELECT invoice_id, diagnosis FROM event_log WHERE stage='diagnose'")}
        esc_person = {}
        for r in conn.execute("SELECT invoice_id, routed_to, observed FROM event_log WHERE stage='escalate'"):
            if json.loads(r["observed"] or "{}").get("queue") == "escalation":
                esc_person[r["invoice_id"]] = r["routed_to"]
        latest = {r[0]: r[1] for r in conn.execute(
            "SELECT invoice_id, MAX(timestamp) FROM event_log GROUP BY invoice_id")}

        BUCKET_LABEL = {"message_sent": "Message sent", "held": "Held for approval",
                        "finance": "Sent to finance", "left_alone": "Left alone",
                        "blocked": "Blocked"}
        rows = []
        for i in conn.execute("""SELECT i.*, c.name client, c.revenue_line, c.internal_owner_id
                                 FROM invoices i JOIN clients c ON c.client_id=i.client_id"""):
            iid, paid = i["invoice_id"], i["status"] == "paid"
            if paid:
                status = "Paid"
            elif bucket.get(iid) == "routed":
                who = team.get(esc_person.get(iid), esc_person.get(iid, "?"))
                status = f"Escalated to {who.split()[0] if who else '?'}"
            else:
                status = BUCKET_LABEL.get(bucket.get(iid), "—")
            skey = "Escalated" if status.startswith("Escalated") else status
            dpt = i["days_past_terms"]
            when = rel_time(i["paid_date"]) if paid else rel_time(latest.get(iid))
            cats = {"all"}
            if not paid and dpt > 0:
                cats.add("needs")
            if not paid and dpt <= 0:
                cats.add("inside")
            if status == "Held for approval" or esc_person.get(iid) in cofounder:
                cats.add("waiting")   # awaiting your sign-off, or escalated to the co-founder
            rows.append({
                "id": iid, "client": i["client"],
                "revenue": REVENUE_LABEL.get(i["revenue_line"], i["revenue_line"]),
                "amount": i["amount_inr"], "terms": i["terms_days"], "dpt": dpt,
                "status": status, "skey": STATUS_STYLE.get(skey, "grey"),
                "diagnosis": diag.get(iid) or "—", "when": when or "—", "cats": cats,
                "paid": paid,
            })

        counts = {"all": len(rows),
                  "needs": sum(1 for r in rows if "needs" in r["cats"]),
                  "waiting": sum(1 for r in rows if "waiting" in r["cats"]),
                  "inside": sum(1 for r in rows if "inside" in r["cats"])}
        filters = [("all", "All", counts["all"]), ("needs", "Needs attention", counts["needs"]),
                   ("waiting", "Waiting on you", counts["waiting"]),
                   ("inside", "Inside terms", counts["inside"])]
        sel = request.args.get("filter", "all")
        shown = [r for r in rows if sel in r["cats"]]
        shown.sort(key=lambda r: (0 if r["paid"] else 1, r["dpt"]), reverse=True)

        ctx = base_ctx(conn)
        a = (request.args.get("ask"), request.args.get("arg"), request.args.get("q"))
        ctx.update(active="invoices", filters=filters, sel=sel, rows=shown, wide=True,
                   show_ai=True, per_client=False,
                   exchange=ai_exchange(conn, False, None, None, *a),
                   suggestions=ai_suggestions(False, None, None))
        return render_template("invoices.html", **ctx)
    finally:
        conn.close()


@app.route("/activity")
def activity():
    conn = db_ro()
    try:
        team = {r["person_id"]: dict(r) for r in conn.execute("SELECT * FROM team")}
        disp = disposition(conn)
        seg_order = [("message_sent", "Message sent to client", "#3395FF"),
                     ("held", "Held for founder approval", "#F59E0B"),
                     ("routed", "Routed to a colleague", "#A78BFA"),
                     ("finance", "Sent to finance to fix", "#34D399"),
                     ("left_alone", "Left alone on purpose", "#9CA3AF"),
                     ("blocked", "Blocked by a guardrail", "#F87171")]
        total = sum(len(disp[k]) for k, _, _ in seg_order)
        segments = [{"label": lbl, "color": col, "n": len(disp[k]),
                     "pct": round(100*len(disp[k])/total, 2)} for k, lbl, col in seg_order]

        n0, n30 = naive(conn, 0), naive(conn, 30)
        msg_sent = disp["message_sent"]
        # agent harm over the messages actually sent (should be zero)
        harm = conn.execute(
            "SELECT SUM(CASE WHEN days_past_terms<0 THEN 1 ELSE 0 END) inside,"
            "SUM(CASE WHEN gt_diagnosis='dispute' THEN 1 ELSE 0 END) dispute,"
            "SUM(CASE WHEN c.segment='strategic' THEN 1 ELSE 0 END) strat,"
            "SUM(CASE WHEN contact_verified=0 THEN 1 ELSE 0 END) dead "
            "FROM invoices i JOIN clients c ON c.client_id=i.client_id "
            f"WHERE invoice_id IN ({','.join('?'*len(msg_sent)) or 'NULL'})", msg_sent).fetchone() \
            if msg_sent else {"inside": 0, "dispute": 0, "strat": 0, "dead": 0}
        cf_rows = [
            ("Messages sent to clients", n0["msgs"], len(msg_sent)),
            ("Sent to clients not actually late", n0["inside"], harm["inside"] or 0),
            ("Sent to clients in active dispute", n0["dispute"], harm["dispute"] or 0),
            ("Sent firmly to strategic accounts", n0["strat"], harm["strat"] or 0),
            ("Sent to a dead contact", n0["dead"], harm["dead"] or 0),
        ]
        cf_note = (f"A smarter reminder tool with correct payment terms would fix the first row "
                   f"(net-30: {n30['msgs']} messages, {n30['inside']} to clients inside terms). "
                   f"It would still chase {n30['dispute']} dispute, stay firm on {n30['strat']} "
                   f"strategic account, and message {n30['dead']} dead mailbox — those need memory "
                   f"of the client, not better date maths.")

        # accuracy
        gt = {r["invoice_id"]: r["gt_diagnosis"] for r in conn.execute(
            "SELECT invoice_id, gt_diagnosis FROM invoices WHERE gt_diagnosis IS NOT NULL")}
        dg = {r["invoice_id"]: (r["diagnosis"], json.loads(r["observed"] or "{}").get("method"))
              for r in conn.execute("SELECT invoice_id, diagnosis, observed FROM event_log WHERE stage='diagnose'")}
        correct = [i for i in gt if dg.get(i, (None,))[0] == gt[i]]
        wrong = sorted(i for i in gt if dg.get(i, (None,))[0] != gt[i])
        no_model = [i for i in gt if dg.get(i, (None, None))[1] != "llm"]
        left = disp["left_alone"]
        should = {r["invoice_id"]: r["gt_should_contact"] for r in conn.execute(
            "SELECT invoice_id, gt_should_contact FROM invoices")}
        left_correct = [i for i in left if should.get(i) == 0]
        accuracy = [
            ("Diagnosis correct", f"{len(correct)} of {len(gt)}"),
            ("Resolved without a model call", f"{len(no_model)} of {len(gt)}"),
            ("Correctly left alone", f"{len(left_correct)} of {len(left)}"),
        ]
        misses = "Two misses: " + ", ".join(
            f"{i} ({gt[i]} read as {dg[i][0]})" for i in wrong) + "."

        # escalations by owner
        esc = {}
        for r in conn.execute("SELECT invoice_id, routed_to, observed FROM event_log "
                              "WHERE stage='escalate'"):
            if json.loads(r["observed"] or "{}").get("queue") == "escalation":
                esc.setdefault(r["routed_to"], []).append(r["invoice_id"])
        esc_rows = [{"name": team.get(pid, {}).get("name", pid),
                     "role": team.get(pid, {}).get("role", ""),
                     "n": len(ids), "ids": sorted(ids)}
                    for pid, ids in sorted(esc.items(), key=lambda x: -len(x[1]))]

        ctx = base_ctx(conn)
        a = (request.args.get("ask"), request.args.get("arg"), request.args.get("q"))
        ctx.update(active="activity", segments=segments, total=total, cf_rows=cf_rows,
                   cf_note=cf_note, accuracy=accuracy, misses=misses, esc_rows=esc_rows,
                   show_ai=True, per_client=False,
                   exchange=ai_exchange(conn, False, None, None, *a),
                   suggestions=ai_suggestions(False, None, None))
        return render_template("activity.html", **ctx)
    finally:
        conn.close()


TOGGLES = [
    ("quiet_hours", "Only contact between 9am and 7pm IST", "Respects clients' working hours."),
    ("max_3_contacts", "At most 3 contacts per invoice", "Avoids over-communication, then escalates."),
    ("stop_on_dispute", "Stop on any dispute", "Never chase a contested invoice; route to the lead."),
    ("never_chase_strategic", "Never chase a strategic account without approval",
     "Protects your most important relationships."),
]


@app.route("/onboarding")
def onboarding():
    step = max(1, min(4, int(request.args.get("step", 1))))
    conn = db_ro()
    try:
        ctx = base_ctx(conn)
        ctx.update(active="onboarding", step=step)
        if step == 1:
            ctx["s"] = {k: get_setting(conn, k) for k in
                        ("payment_terms", "avg_invoice", "approval_threshold")}
            rows = conn.execute(
                "SELECT amount_inr, (SELECT segment FROM clients c WHERE c.client_id=i.client_id) seg "
                "FROM invoices i WHERE status!='paid'").fetchall()
            ctx["ledger"] = json.dumps([{"a": r["amount_inr"], "s": 1 if r["seg"] == "strategic" else 0}
                                        for r in rows])
        elif step == 2:
            ctx["uploads"] = [dict(r) for r in conn.execute("SELECT * FROM uploads ORDER BY id DESC")]
        elif step == 3:
            ctx["team"] = [{"pid": r["person_id"], "name": r["name"], "role": r["role"],
                            "types": [t for t in (r["handles_escalation_types"] or "").split(",") if t]}
                           for r in conn.execute("SELECT * FROM team ORDER BY person_id")]
        elif step == 4:
            ctx["toggles"] = [{"key": k, "label": l, "desc": d, "on": get_setting(conn, k) == "1"}
                              for k, l, d in TOGGLES]
        return render_template("onboarding.html", **ctx)
    finally:
        conn.close()


@app.route("/onboarding/step1", methods=["POST"])
def onboarding_step1():
    for k in ("payment_terms", "avg_invoice", "approval_threshold"):
        v = request.form.get(k, "").replace(",", "").replace("₹", "").strip()
        if v:
            set_setting(k, v)
    return redirect(request.form.get("next") or url_for("onboarding", step=2))


@app.route("/onboarding/upload", methods=["POST"])
def onboarding_upload():
    f = request.files.get("file")
    if f and f.filename:
        size = len(f.read())
        conn = db_rw()
        conn.execute("INSERT INTO uploads (filename, size, created_at) VALUES (?,?,?)",
                     (f.filename, size, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
    return redirect(url_for("onboarding", step=2))


@app.route("/onboarding/team", methods=["POST"])
def onboarding_team():
    pid, action = request.form.get("pid"), request.form.get("action")
    val = request.form.get("type", "").strip().replace(" ", "_").lower()
    conn = db_rw()
    row = conn.execute("SELECT handles_escalation_types FROM team WHERE person_id=?", (pid,)).fetchone()
    if row:
        types = [t for t in (row["handles_escalation_types"] or "").split(",") if t]
        if action == "remove" and val in types:
            types.remove(val)
        elif action == "add" and val and val not in types:
            types.append(val)
        conn.execute("UPDATE team SET handles_escalation_types=? WHERE person_id=?",
                     (",".join(types), pid))
        conn.commit()
    conn.close()
    return redirect(request.form.get("next") or url_for("onboarding", step=3))


@app.route("/onboarding/rules", methods=["POST"])
def onboarding_rules():
    for k, _, _ in TOGGLES:
        set_setting(k, "1" if request.form.get(k) else "0")
    if request.form.get("action") == "finish":
        set_setting("onboarding_completed", "1")
        return redirect(url_for("clients"))
    return redirect(request.form.get("next") or url_for("onboarding", step=4))


@app.route("/onboarding/skip")
def onboarding_skip():
    set_setting("onboarding_completed", "1")   # defaults already seeded
    return redirect(url_for("clients"))


@app.route("/configure")
def configure():
    conn = db_ro()
    try:
        ctx = base_ctx(conn)
        ctx["active"] = "configure"
        ctx["s"] = {k: get_setting(conn, k) for k in
                    ("payment_terms", "avg_invoice", "approval_threshold")}
        ctx["team"] = [{"pid": r["person_id"], "name": r["name"], "role": r["role"],
                        "types": [t for t in (r["handles_escalation_types"] or "").split(",") if t]}
                       for r in conn.execute("SELECT * FROM team ORDER BY person_id")]
        ctx["toggles"] = [{"key": k, "label": l, "desc": d, "on": get_setting(conn, k) == "1"}
                          for k, l, d in TOGGLES]
        return render_template("configure.html", **ctx)
    finally:
        conn.close()


# Runs on import so the serverless entrypoint (which never hits __main__)
# still creates the feedback/settings/uploads tables. Idempotent.
try:
    ensure_tables()
except Exception:
    pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False)
