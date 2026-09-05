"""Post-call email summaries via Resend.

After a voice call is ingested, compose a short manager-readable summary and send
it. Constraint: the Resend sandbox sender (onboarding@resend.dev) only delivers to
the account owner, so EVERY recipient is overridden to DEMO_EMAIL. The team's
example.com addresses appear in the body as "Would route to …", never as real
To/Cc.

  compose(conn, invoice_id) -> dict(subject, body, outcome, recipient, would_route)
  send(conn, invoice_id)     -> compose + Resend send + log email_sent / email_failed
"""
import os
import re

from decide import decide
from log import connect, write_event
from voice_bridge import _load, _latest_diagnosis
from policy import CONTACT_CAP

_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]


def _fmt_date(iso):
    if not iso:
        return None
    y, m, d = iso.split("-")
    return f"{int(d)} {_MON[int(m)]}"


def _team(conn):
    return {r["person_id"]: dict(r) for r in conn.execute("SELECT * FROM team")}


def _who(team, pid):
    t = team.get(pid, {})
    return f"{t.get('name', pid)} ({t.get('role', '')})".replace(" ()", "")


def _key_lines(transcript, limit=4):
    """A few key client lines from the transcript, not the whole thing."""
    if not transcript:
        return []
    # Split into speaker turns if labelled, else sentences.
    turns = re.split(r"(?=(?:Asha|Agent|Client|User|Customer)\s*:)", transcript)
    turns = [t.strip() for t in turns if t.strip()]
    client = [t for t in turns if re.match(r"(Client|User|Customer)\s*:", t)]
    picked = client or turns
    if not picked:  # no labels at all → sentences
        picked = [s.strip() for s in re.split(r"(?<=[.?!])\s+", transcript) if s.strip()]
    out = []
    for t in picked[:limit]:
        t = re.sub(r"^(Asha|Agent|Client|User|Customer)\s*:\s*", "", t).strip()
        if t:
            out.append(t if len(t) <= 120 else t[:117] + "…")
    return out


def _outcome(promise_date, transcript):
    t = (transcript or "").lower()
    if promise_date:
        return f"Promise captured, {_fmt_date(promise_date)}"
    if any(w in t for w in ("dispute", "not agreed", "we never", "disagree", "wrong amount")):
        return "Dispute raised"
    if any(w in t for w in ("stop chasing", "don't call", "harass", "ridiculous")):
        return "Escalated"
    if not t.strip() or "no answer" in t or "voicemail" in t:
        return "Client unavailable"
    return "No commitment"


def compose(conn, invoice_id):
    inv = _load(conn, invoice_id)
    if not inv:
        raise SystemExit(f"{invoice_id} not found")
    team = _team(conn)
    diagnosis = _latest_diagnosis(conn, invoice_id)

    # The completed call (from ingest_transcript).
    call = conn.execute(
        "SELECT voice_transcript, voice_promise_captured FROM event_log WHERE invoice_id=? "
        "AND action_taken='voice_call' ORDER BY event_id DESC LIMIT 1", (invoice_id,)).fetchone()
    transcript = call["voice_transcript"] if call else ""
    captured = call["voice_promise_captured"] if call else None      # e.g. 'pending 2026-09-11'
    promise_date = None
    if captured and captured.startswith("pending "):
        promise_date = captured.split(" ", 1)[1]

    # Why the call happened: the voice_call decision (rule + register_reason).
    vd = conn.execute(
        "SELECT rule_applied, register, register_reason FROM event_log WHERE invoice_id=? "
        "AND stage='decide' AND decision='voice_call' ORDER BY event_id DESC LIMIT 1",
        (invoice_id,)).fetchone()
    raw_rule = vd["rule_applied"] if vd else "contact cap reached -> voice call"
    rule = re.sub(r"^override \d+:\s*", "", raw_rule).replace("->", "→").strip()
    if vd:
        register, reg_reason = vd["register"], vd["register_reason"]
    else:  # no voice_call decision recorded — fall back to the invoice's register
        fb = decide(inv, diagnosis, 0)
        register, reg_reason = fb["register"], fb["register_reason"]

    # Would-route: the rung-5 human this invoice escalates to (owner of the outcome),
    # cc the co-founder. Amber/green -> Snehal; red -> Aditya.
    esc = "aditya" if inv["tier"] == "red" else "snehal"
    route_to = _who(team, esc)
    would_route = f"Would route to: {route_to}"
    if esc != "aditya":
        would_route += f" · cc {_who(team, 'aditya')}"

    # What happens next: the post-call decision the pipeline actually takes.
    cap = CONTACT_CAP["strategic" if inv["segment"] == "strategic" else "standard"]
    post = decide(inv, diagnosis, cap, voice_done=True)
    if post["action"] == "do_nothing":
        next_line = f"Next: hold — {post['routed_reason']}."
    elif post["action"] == "escalate":
        next_line = f"Next: escalate to {_who(team, post['routed_to']).split(' (')[0]} — {post['routed_reason']}."
    else:
        next_line = f"Next: {post['action']} — {post['routed_reason']}."

    outcome = _outcome(promise_date, transcript)
    amount = inv["amount_inr"] - inv["amount_paid_inr"]
    extracted = (f"Promise to pay ₹{amount:,} by {_fmt_date(promise_date)}."
                 if promise_date else "No commitment given.")
    client_lines = _key_lines(transcript)

    subject = f"{inv['client_name']} · {invoice_id} · {outcome}"

    lines = [
        would_route,
        "",
        f"Why the call: {rule}. Register: {register}" + (f" — {reg_reason}." if reg_reason else "."),
        "",
        "What the client said:",
    ]
    lines += [f"  • {l}" for l in client_lines] or ["  • (no transcript captured)"]
    lines += [
        "",
        f"Extracted: {extracted}",
        next_line,
    ]
    # Tier before/after only if it changed (profile_delta on the call, if any).
    # (No tier change in the standard voice flow, so this line is usually omitted.)
    lines += [
        "",
        "Go to the dashboard for details.",
    ]
    body = "\n".join(lines)
    return {"subject": subject, "body": body, "outcome": outcome,
            "recipient": os.environ.get("DEMO_EMAIL", ""), "would_route": would_route}


def send(conn, invoice_id, dry_run=False):
    from log import write_event
    msg = compose(conn, invoice_id)
    inv = conn.execute("SELECT client_id FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    client_id = inv[0] if inv else invoice_id
    to = os.environ.get("DEMO_EMAIL", "")          # override EVERY recipient
    from_email = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")

    if dry_run:
        return {**msg, "sent": False, "dry_run": True}

    try:
        import resend
        resend.api_key = os.environ["RESEND_API_KEY"]
        resp = resend.Emails.send({"from": from_email, "to": [to],
                                   "subject": msg["subject"], "text": msg["body"]})
        rid = resp.get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
        write_event(conn, invoice_id=invoice_id, client_id=client_id, stage="execute",
                    action_taken="email_sent", channel="email", outcome="sent",
                    message_sent=msg["subject"],
                    observed={"recipient": to, "subject": msg["subject"], "resend_id": rid})
        return {**msg, "sent": True, "resend_id": rid}
    except Exception as exc:
        detail = str(exc)[:300]
        write_event(conn, invoice_id=invoice_id, client_id=client_id, stage="execute",
                    action_taken="email_failed", channel="email",
                    outcome=f"email_failed: {detail[:120]}",
                    observed={"recipient": to, "subject": msg["subject"], "error": detail})
        return {**msg, "sent": False, "error": detail}


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    conn = connect()
    try:
        inv_id = sys.argv[1] if len(sys.argv) > 1 else "INV-23"
        dry = "--send" not in sys.argv
        res = send(conn, inv_id, dry_run=dry)
        print("SUBJECT:", res["subject"])
        print("-" * 60)
        print(res["body"])
        print("-" * 60)
        print("recipient (override):", res["recipient"])
        print("sent:", res.get("sent"), "| resend_id:", res.get("resend_id"), "| error:", res.get("error"))
    finally:
        conn.close()
