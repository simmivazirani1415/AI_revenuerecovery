"""Voice bridge — rung 4 of the escalation ladder (demonstration level, not full
telephony).

Two halves of the same contract the WhatsApp path already honours, but for a
Vapi voice agent instead of a text draft:

  build_vapi_prompt(invoice_id)
      Compose the voice agent's SYSTEM PROMPT from the real client profile and the
      register + register_reason that decide.py chose. Same inputs as
      execute.draft(); different output format (a spoken-agent brief, not an SMS).

  ingest_transcript(invoice_id, transcript_text)
      Take a Vapi call transcript, pull out any promise + date using the shared
      extractor (extract_promise, the same logic we want the WhatsApp path to use),
      write a voice_transcript event, and update the promise register on the
      invoice — exactly as capturing a promise from a text reply would.

Run:  voice_bridge.py prompt INV-23
      voice_bridge.py prompt INV-45
      voice_bridge.py ingest INV-23 "<transcript text>"
"""
import re
import sys
from datetime import date, timedelta

from decide import decide
from log import connect, write_event

# Anchor for relative dates ("next Friday", "the 18th"). Today per the sim clock.
TODAY = date(2026, 9, 5)

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
# Pay-intent markers across the languages the voice agent actually speaks
# (English + Hinglish/Devanagari + Urdu script — the transcriber bleeds between them).
PAY_INTENT = ("pay", "clear", "settle", "transfer", "send", "release", "wire",
              "remit", "sort it", "make the payment", "do the payment",
              "क्लियर", "पेमेंट", "कर देंगे", "कर दूँगा", "भर देंगे", "चुकता",
              "کلیئر", "پیمنٹ", "کر دیں گے", "ادائیگی")
# Commitment markers ("by/will" and their Hinglish/Urdu equivalents).
COMMIT_WORDS = ("by", "will", "promise", "latest", "तक", "देंगे", "करेंगे",
                "पक्का", "for sure", "फॉर श्योर", "تک", "کر دیں گے", "پکا")


# ---------------------------------------------------------------------------
# Shared promise/date extraction (the logic both channels should use)
# ---------------------------------------------------------------------------
def _next_weekday(anchor, wd):
    delta = (wd - anchor.weekday()) % 7
    return anchor + timedelta(days=delta or 7)   # always the upcoming one


def _parse_date(text):
    """Return (iso_date, matched_phrase) for the first date-like phrase, else None."""
    t = text.lower()

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)                      # 2026-09-18
    if m:
        return m.group(0), m.group(0)

    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+of\s+([a-z]+)", t)  # 18th of September
    if not m:
        m = re.search(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)  # September 18
        if m and m.group(1) in MONTHS:
            mon, day = MONTHS[m.group(1)], int(m.group(2))
            yr = TODAY.year + (1 if mon < TODAY.month else 0)
            return date(yr, mon, day).isoformat(), m.group(0)
    else:
        day, mon = int(m.group(1)), MONTHS.get(m.group(2))
        if mon:
            yr = TODAY.year + (1 if mon < TODAY.month else 0)
            return date(yr, mon, day).isoformat(), m.group(0)

    for name, wd in WEEKDAYS.items():                                  # (next) Friday
        if re.search(rf"\b(next\s+)?{name}\b", t):
            return _next_weekday(TODAY, wd).isoformat(), name

    for name, wd in {"शुक्रवार": 4, "फ्राइडे": 4, "जुमा": 4, "جمعہ": 4,
                     "सोमवार": 0, "मंगलवार": 1, "बुधवार": 2, "गुरुवार": 3,
                     "शनिवार": 5, "रविवार": 6}.items():                  # Devanagari/Urdu weekdays
        if name in t:
            return _next_weekday(TODAY, wd).isoformat(), name

    if "day after tomorrow" in t:
        return (TODAY + timedelta(days=2)).isoformat(), "day after tomorrow"
    if "tomorrow" in t:
        return (TODAY + timedelta(days=1)).isoformat(), "tomorrow"
    if "end of the month" in t or "end of month" in t or "month end" in t:
        nm = date(TODAY.year + (TODAY.month == 12), (TODAY.month % 12) + 1, 1)
        return (nm - timedelta(days=1)).isoformat(), "end of the month"
    if "next week" in t:
        return (TODAY + timedelta(days=7)).isoformat(), "next week"
    if "next month" in t:
        nm = date(TODAY.year + (TODAY.month == 12), (TODAY.month % 12) + 1, 15)
        return nm.isoformat(), "next month"

    m = re.search(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b", t)            # "the 18th"
    if m:
        day = int(m.group(1))
        mon, yr = TODAY.month, TODAY.year
        if day <= TODAY.day:                                          # already passed -> next month
            mon = (TODAY.month % 12) + 1
            yr = TODAY.year + (TODAY.month == 12)
        return date(yr, mon, day).isoformat(), m.group(0)
    return None


def extract_promise(text):
    """Shared with the WhatsApp path: find a payment promise + date in free text.

    Returns dict {promised: bool, date: iso|None, intent: matched|None,
    phrase: matched date phrase|None}.
    """
    t = (text or "").lower()
    intent = next((w for w in PAY_INTENT if w in t), None)
    parsed = _parse_date(t)
    promised = bool(parsed) and (intent is not None
                                 or any(w in t for w in COMMIT_WORDS))
    return {
        "promised": promised,
        "date": parsed[0] if parsed else None,
        "intent": intent,
        "phrase": parsed[1] if parsed else None,
    }


# ---------------------------------------------------------------------------
# build_vapi_prompt
# ---------------------------------------------------------------------------
def _load(conn, invoice_id):
    return dict(conn.execute(
        """SELECT ai.*, c.tier, c.segment, c.internal_owner_id, c.name AS client_name,
                  c.referred_by, c.referrals_made, c.voice_permitted,
                  c.promises_broken, c.promises_kept, c.invoices_paid, c.worst_days_late
           FROM agent_invoices ai JOIN clients c ON c.client_id = ai.client_id
           WHERE ai.invoice_id = ?""", (invoice_id,)).fetchone())


def _latest_diagnosis(conn, invoice_id):
    r = conn.execute("SELECT diagnosis FROM event_log WHERE stage='diagnose' "
                     "AND invoice_id=? ORDER BY event_id DESC LIMIT 1", (invoice_id,)).fetchone()
    return r[0] if r else None


TONE = {
    "warm": ("Warm and understanding. Acknowledge the cash pressure, do not pressure "
             "or threaten. Offer the instalment plan as a genuine help. Hinglish is fine "
             "if it feels natural."),
    "neutral": ("Professional and matter-of-fact. Be clear about the invoice and the "
                "ask; no warmth theatre, no pressure."),
    "firm": ("Firm and direct. Make clear this has been chased repeatedly and is now "
             "escalating internally. Offer no new concessions. Stay professional and "
             "civil — never hostile or threatening."),
}


def _inr(n):
    """Indian digit grouping: 300000 -> '3,00,000'."""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return f"{head},{tail}"


def _amount_words(n):
    """Spoken Indian form: 300000 -> '3 lakh rupees', 280000 -> '2 lakh 80 thousand rupees'."""
    n = int(n)
    lakh, rem = n // 100000, n % 100000
    thou = rem // 1000
    parts = []
    if lakh:
        parts.append(f"{lakh} lakh")
    if thou:
        parts.append(f"{thou} thousand")
    if not parts:
        parts.append(str(n))
    return " ".join(parts) + " rupees"


def build_vapi_prompt(invoice_id):
    conn = connect()
    try:
        inv = _load(conn, invoice_id)
        if not inv:
            raise SystemExit(f"{invoice_id} not found")
        diagnosis = _latest_diagnosis(conn, invoice_id)
        # Register + register_reason are what decide.py chose (same source as draft()).
        d = decide(inv, diagnosis, inv_contacts := 0)
        register, reason = d["register"], d["register_reason"]
        outstanding = inv["amount_inr"] - inv["amount_paid_inr"]
        amount_disp = _inr(outstanding)
        amount_words = _amount_words(outstanding)
        history = (f"paid {inv['invoices_paid']} before, "
                   f"{inv['promises_broken']} broken / {inv['promises_kept']} kept promises, "
                   f"worst {inv['worst_days_late']}d late")
        prompt = f"""SYSTEM PROMPT — Vapi voice agent  (invoice {invoice_id})

You are "Asha", a female accounts assistant calling on behalf of Vantage Lab (a
Mumbai marketing agency). You are calling {inv['client_name']}.

PERSONA & LANGUAGE (do not deviate)
  - You are a woman. Use feminine Hindi verb forms consistently — "मैं बोल रही हूँ",
    "मैं नोट कर लूँगी", "मैं अपडेट कर दूँगी". Never use masculine forms and never say
    both at once (no "रहा रही", no "दूँगा दूँगी", no "लेता लेती").
  - Speak Hindi / Hinglish in Devanagari and Latin script only. Do NOT use Urdu
    (Arabic) script at any point, even for loanwords like shukriya.

WHO YOU ARE CALLING
  Client:   {inv['client_name']}  ({inv['tier']} tier, {inv['segment']} account)
  History:  {history}
  Owner:    internal contact is {inv['internal_owner_id']}

WHY YOU ARE CALLING
  Invoice:  {invoice_id} — ₹{amount_disp} outstanding, {max(0, inv['days_past_terms'])} days past terms.
  Read:     the agent's diagnosis is "{diagnosis}".
  This is the voice rung: it follows repeated messages that went unanswered or unresolved.
  Payment link to reference if useful: {inv['rzp_link_url'] or '(none on file)'}

THE AMOUNT (say it correctly)
  The outstanding amount is exactly ₹{amount_disp} — say it as "{amount_words}".
  State the amount at most once. Do NOT convert it, round it, drop or add zeros, or
  restate it in a different figure. If unsure, refer to "invoice {invoice_id}" without
  a number rather than guess.

TONE — register "{register}"  ({reason})
  {TONE.get(register, TONE['neutral'])}

YOUR OBJECTIVE
  Get a concrete commitment: a specific amount and a specific date the client will pay.
  If they cannot pay in full, and the register allows, offer to split it into instalments.

RULES
  - Capture any promise and the exact date they give, verbatim, and read it back to confirm.
  - If they dispute the invoice or the work, stop pushing — acknowledge it and say it will
    be routed to their account owner. Do not argue the dispute on the call.
  - Never threaten, never raise your voice, keep the call under two minutes.
  - Stay in the feminine, stay in Devanagari/Latin script, and keep the amount exact.
  - End by confirming the agreed date (or that there is no commitment).
"""
        return prompt
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ingest_transcript
# ---------------------------------------------------------------------------
def ingest_transcript(invoice_id, transcript_text):
    conn = connect()
    try:
        inv = _load(conn, invoice_id)
        if not inv:
            raise SystemExit(f"{invoice_id} not found")
        promise = extract_promise(transcript_text)
        captured = (f"pending {promise['date']}" if promise["promised"] and promise["date"]
                    else None)

        write_event(conn, invoice_id=invoice_id, client_id=inv["client_id"],
                    stage="execute", action_taken="voice_call", channel="voice",
                    outcome=("voice_promise_captured" if captured else "voice_no_promise"),
                    voice_transcript=transcript_text, voice_promise_captured=captured,
                    observed={"source": "vapi", "intent": promise["intent"],
                              "date_phrase": promise["phrase"]})

        # Update the promise register — same effect as capturing a text promise.
        if promise["promised"] and promise["date"]:
            conn.execute("UPDATE invoices SET promise_status='pending', promise_date=? "
                         "WHERE invoice_id=?", (promise["date"], invoice_id))
            conn.commit()
        return {"invoice_id": invoice_id, "promise": promise, "captured": captured}
    finally:
        conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "prompt":
        print(build_vapi_prompt(sys.argv[2]))
    elif cmd == "ingest":
        import json
        print(json.dumps(ingest_transcript(sys.argv[2], sys.argv[3]), indent=2))
