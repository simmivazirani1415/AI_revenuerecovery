"""Inbound Twilio WhatsApp webhook.

Receives Twilio's form-encoded webhook, parses the sender and body, matches the
reply to the most recently contacted invoice for that number, and writes an
execute-stage event capturing the inbound reply.

Dev note: the Twilio sandbox only messages your own joined number, so in
development every client shares one number and we match to the most recent
outbound send. In production, clients carry distinct numbers and we'd match on
the sender's number -> client -> their open invoices.

Run:   ./venv/bin/python inbound.py         (listens on :5001)
Expose: ngrok http 5001                      (paste the https URL + /webhook into Twilio)
"""

from flask import Flask, request

from diagnose import diagnose_one
from log import connect, write_event
from voice_bridge import ingest_transcript

app = Flask(__name__)


@app.get("/")
def health():
    return "receivables inbound webhook: OK. POST /webhook", 200


@app.post("/webhook")
def webhook():
    sender = (request.form.get("From") or "").replace("whatsapp:", "").strip()
    body = (request.form.get("Body") or "").strip()

    conn = connect()
    try:
        # Most recently contacted invoice (dev: single shared number).
        row = conn.execute(
            "SELECT invoice_id, client_id FROM event_log "
            "WHERE stage = 'execute' AND outcome = 'sent' AND channel = 'whatsapp' "
            "ORDER BY timestamp DESC, event_id DESC LIMIT 1").fetchone()

        if not row:
            app.logger.info("Inbound from %s but no prior outbound to match: %r",
                            sender, body)
            return "<Response/>", 200, {"Content-Type": "application/xml"}

        invoice_id, client_id = row
        write_event(
            conn, invoice_id=invoice_id, client_id=client_id, stage="execute",
            action_taken="received_reply", channel="whatsapp",
            outcome="inbound_reply",
            observed={"direction": "inbound", "from": sender, "reply_text": body})

        # The reply is new signal: update the invoice and re-diagnose it.
        conn.execute("UPDATE invoices SET reply_text = ? WHERE invoice_id = ?",
                     (body, invoice_id))
        conn.commit()
        rediag = diagnose_one(conn, invoice_id)
        app.logger.info("Inbound from %s matched to %s: %r -> re-diagnosed %s",
                        sender, invoice_id, body, rediag)
    finally:
        conn.close()

    return "<Response/>", 200, {"Content-Type": "application/xml"}


@app.post("/vapi-webhook")
def vapi_webhook():
    """Vapi end-of-call-report -> ingest the transcript and close the loop.

    Maps the call to an invoice via call metadata if present, else the most
    recent voice_call_initiated event (dev: single shared assistant)."""
    data = request.get_json(force=True, silent=True) or {}
    msg = data.get("message", {}) or {}
    if msg.get("type") != "end-of-call-report":
        return ("", 204)  # ignore status-update / transcript / etc.

    artifact = msg.get("artifact", {}) or {}
    transcript = artifact.get("transcript") or msg.get("transcript") or ""
    call = msg.get("call", {}) or {}
    meta = call.get("metadata", {}) or msg.get("metadata", {}) or {}
    invoice_id = meta.get("invoiceId")

    conn = connect()
    try:
        if not invoice_id:
            row = conn.execute(
                "SELECT invoice_id FROM event_log WHERE stage='execute' "
                "AND action_taken='voice_call_initiated' "
                "ORDER BY event_id DESC LIMIT 1").fetchone()
            invoice_id = row[0] if row else None
    finally:
        conn.close()

    if not invoice_id or not transcript:
        app.logger.info("vapi-webhook: no invoice_id or empty transcript; skipping")
        return ("", 200)

    res = ingest_transcript(invoice_id, transcript)   # writes voice_transcript + updates promise
    app.logger.info("vapi-webhook: %s -> captured %s", invoice_id, res.get("captured"))
    return ("", 200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
