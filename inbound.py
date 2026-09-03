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

from log import connect, write_event

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
        app.logger.info("Inbound from %s matched to %s: %r", sender, invoice_id, body)
    finally:
        conn.close()

    return "<Response/>", 200, {"Content-Type": "application/xml"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
