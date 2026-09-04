"""The agent's audit log: one helper to write an event, one to read a history.

Every pipeline stage (detect / diagnose / decide / execute / escalate) calls
write_event() with everything it knows so far. Rows are denormalized on
purpose: any single row reconstructs the whole decision without a join.

    from log import connect, write_event, get_history

    conn = connect()
    write_event(conn, invoice_id="INV-23", client_id="brightline",
                stage="execute", observed={...}, diagnosis="cash_stress", ...)
    history = get_history(conn, "INV-23")
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "agency.db")

VALID_STAGES = {"detect", "diagnose", "decide", "execute", "escalate"}

# Columns write_event accepts, in table order (event_id/timestamp handled apart).
_FIELDS = [
    "invoice_id", "client_id", "stage", "observed", "diagnosis",
    "diagnosis_evidence", "decision", "rule_applied", "register",
    "register_reason", "action_taken", "message_sent", "channel",
    "outcome", "outcome_at", "routed_to", "routed_reason", "profile_delta",
    "voice_transcript", "voice_promise_captured",
]
# Structured columns stored as JSON text.
_JSON_FIELDS = {"observed", "profile_delta"}


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _encode(field, value):
    if value is None:
        return None
    if field in _JSON_FIELDS and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def write_event(conn, *, invoice_id, client_id, stage, timestamp=None, **fields):
    """Append one event. Returns the new event_id.

    Required: invoice_id, client_id, stage. Everything else is optional so a
    given stage fills only what it knows — but pass the full accumulated
    context if you want each row to stand alone. dict/list values for
    `observed` and `profile_delta` are JSON-encoded automatically.
    """
    if stage not in VALID_STAGES:
        raise ValueError(f"stage must be one of {sorted(VALID_STAGES)}, got {stage!r}")

    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise ValueError(f"unknown event fields: {sorted(unknown)}")

    row = {
        "invoice_id": invoice_id,
        "client_id": client_id,
        "stage": stage,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for f in _FIELDS:
        if f in ("invoice_id", "client_id", "stage"):
            continue
        row[f] = _encode(f, fields.get(f))

    cols = list(row.keys())
    sql = (f"INSERT INTO event_log ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)})")
    cur = conn.execute(sql, [row[c] for c in cols])
    conn.commit()
    return cur.lastrowid


def get_history(conn, invoice_id):
    """Return the full, chronologically ordered event history for one invoice.

    JSON columns are decoded back into Python objects.
    """
    rows = conn.execute(
        "SELECT * FROM event_log WHERE invoice_id = ? ORDER BY event_id",
        (invoice_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for f in _JSON_FIELDS:
            if d.get(f):
                try:
                    d[f] = json.loads(d[f])
                except (TypeError, ValueError):
                    pass
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Proof: one row reconstructs the whole decision. Writes a dummy event inside
# a transaction, prints it as a self-contained narrative, then rolls back so
# the real log stays empty.
# ---------------------------------------------------------------------------
def _demo():
    conn = connect()
    try:
        event_id = write_event(
            conn,
            timestamp="2026-09-01T10:15:00+00:00",
            invoice_id="INV-23",
            client_id="brightline",
            stage="execute",
            observed={
                "client": "Brightline Retail", "tier": "amber",
                "amount_inr": 300000, "terms_days": 45, "days_past_terms": 15,
                "reply_text": "We're facing a cashflow crunch this quarter.",
                "response_rate": 0.45, "promises_broken": 0,
                "sibling_invoices_slipping": ["INV-24"],
            },
            diagnosis="cash_stress",
            diagnosis_evidence="Reply mentions cashflow; INV-24 slipping in "
                               "parallel; lateness worse than Brightline's norm.",
            decision="offer_payment_plan",
            rule_applied="cash_stress -> offer the founder's payment-plan template",
            register="warm",
            register_reason="Genuine cash stress, amber not red - solve, don't pressure.",
            action_taken="Sent payment-plan offer with test-mode link",
            message_sent="Hi Brightline - noticed things are tight this quarter. "
                         "Happy to split INV-23 (Rs 3,00,000) over 3 instalments. "
                         "Pay here when ready: https://rzp.io/rzp/vrnPlf3",
            channel="whatsapp",
            outcome="delivered_awaiting_reply",
            outcome_at="2026-09-01T10:15:04+00:00",
            routed_to="faiz",
            routed_reason="Faiz Ahmed owns Brightline (Delivery, Mobile & Web).",
            profile_delta={"register": "neutral -> warm",
                           "watchlist": "added: cash_stress cluster (INV-23,INV-24)"},
        )

        [row] = get_history(conn, "INV-23")

        print(f"Reconstructing the full decision from event_log row #{event_id} alone")
        print("(no other table opened):\n")
        o = row["observed"]
        print(f"  WHEN      {row['timestamp']}")
        print(f"  WHAT      invoice {row['invoice_id']} for {o['client']} "
              f"({row['client_id']}), tier={o['tier']}")
        print(f"  STAGE     {row['stage']}")
        print(f"  SAW       Rs {o['amount_inr']:,} on net-{o['terms_days']}, "
              f"{o['days_past_terms']}d past terms; reply: \"{o['reply_text']}\"")
        print(f"            response_rate={o['response_rate']}, "
              f"also slipping: {o['sibling_invoices_slipping']}")
        print(f"  DIAGNOSED {row['diagnosis']}  <-  {row['diagnosis_evidence']}")
        print(f"  DECIDED   {row['decision']}  (rule: {row['rule_applied']})")
        print(f"  REGISTER  {row['register']}  <-  {row['register_reason']}")
        print(f"  DID       {row['action_taken']}  via {row['channel']}")
        print(f"  SENT      \"{row['message_sent']}\"")
        print(f"  OUTCOME   {row['outcome']} @ {row['outcome_at']}")
        print(f"  ROUTED    {row['routed_to']}  <-  {row['routed_reason']}")
        print(f"  PROFILE   {row['profile_delta']}")
    finally:
        # write_event commits (events must be durable), so clean up the demo
        # row explicitly to leave the real log empty.
        conn.execute("DELETE FROM event_log WHERE event_id = ?", (event_id,))
        conn.commit()
        conn.close()


if __name__ == "__main__":
    _demo()
