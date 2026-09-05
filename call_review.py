"""Call review surface for the founder — list, detail, and a computed review.

A "call" is a voice_call event (action_taken='voice_call') carrying the transcript
and any captured promise, plus the voice_call_placed/override event (register,
brief, duration, cost) and the voice_call decision (why it fired).
"""
import json
import re
from datetime import date

from decide import decide
from voice_bridge import _load, _latest_diagnosis, build_vapi_prompt
from policy import CONTACT_CAP

_AGENT = ("asha", "agent")
_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]
_WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def fmt_date(iso):
    if not iso:
        return None
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{d} {_MON[m]}"


def parse_turns(transcript):
    """[(speaker, text)] where speaker is 'agent' or 'client'."""
    parts = re.split(r"(?=(?:Asha|Agent|Client|User|Customer)\s*:)", transcript or "")
    turns = []
    for p in (x.strip() for x in parts):
        if not p:
            continue
        m = re.match(r"(Asha|Agent|Client|User|Customer)\s*:\s*(.*)", p, re.S)
        if m:
            spk = "agent" if m.group(1).lower() in _AGENT else "client"
            turns.append((spk, m.group(2).strip()))
        else:
            turns.append(("agent", p))
    return turns


def _promise_date(captured):
    if captured and captured.startswith("pending "):
        return captured.split(" ", 1)[1]
    return None


def review(transcript, promise_date, amount_inr):
    """Compute the founder review: metrics + 'what to improve' flags."""
    turns = parse_turns(transcript)
    agent = [t for s, t in turns if s == "agent"]
    client = [t for s, t in turns if s == "client"]
    aw = sum(len(t.split()) for t in agent)
    cw = sum(len(t.split()) for t in client)
    total = aw + cw or 1
    talk_ratio = round(aw / total * 100)
    longest = max((len(t.split()) for t in agent), default=0)

    # Date tokens for confirmation-counting.
    toks = []
    if promise_date:
        y, m, d = (int(x) for x in promise_date.split("-"))
        toks = [str(d), _MON[m].lower(), _WD[date(y, m, d).weekday()].lower()]
    conf_idx = [i for i, (s, t) in enumerate(turns)
                if s == "agent" and ("confirm" in t.lower()
                                     or any(tok and tok in t.lower() for tok in toks))]
    confirmations = len(conf_idx)

    tl = transcript.lower() if transcript else ""
    kept_talking = bool(conf_idx) and any(
        s == "agent" and not re.search(r"\b(thank|thanks|bye|goodbye|take care)\b", t.lower())
        for s, t in turns[conf_idx[-1] + 1:])

    improve = []
    got = bool(promise_date)
    if not got:
        improve.append("No commitment captured — the call ended without a date.")
    if confirmations > 1:
        improve.append(f"Confirmed the date {confirmations} times — should read it back once, then stop.")
    if any(w in tl for w in ("discount", "waive", "% off", "percent off", "knock off", "reduce the")):
        improve.append("Offered a concession beyond the 50/50 split.")
    if any(w in tl for w in ("legal", "court", "terminate", "cut off", "penalty", "late fee", "consequence")):
        improve.append("Mentioned consequences — out of register for a recovery call.")
    if kept_talking:
        improve.append("Kept talking after the confirmation instead of closing.")
    if talk_ratio > 70:
        improve.append(f"Agent talk ratio {talk_ratio}% — over 70%, let the client speak more.")
    if longest > 60:
        improve.append(f"Longest agent turn {longest} words — over 60, tighten it.")
    if re.search(r"[ऀ-ॿ؀-ۿ]", transcript or ""):
        improve.append("Non-English script in the transcript (script bleed).")
    if any(re.search(r"\d{4,}", t) for t in agent):
        improve.append("Amount spoken as digits, not words (possible misread).")

    return {
        "got_commitment": got,
        "confirmations": confirmations,
        "talk_ratio": talk_ratio, "talk_ratio_flag": talk_ratio > 70,
        "longest_agent_turn": longest, "longest_flag": longest > 60,
        "agent_words": aw, "client_words": cw,
        "improve": improve,
    }


def _outcome(promise_date, transcript):
    tl = (transcript or "").lower()
    if promise_date:
        return f"Promise captured, {fmt_date(promise_date)}"
    if any(w in tl for w in ("dispute", "disagree", "wrong amount", "we never")):
        return "Dispute raised"
    if any(w in tl for w in ("stop chasing", "don't call", "ridiculous")):
        return "Escalated"
    if not tl.strip() or "voicemail" in tl or "no answer" in tl:
        return "Client unavailable"
    return "No commitment"


def _duration_cost(observed):
    dur = observed.get("duration_sec")
    cost = observed.get("cost")
    dur_s = f"{int(dur // 60)}m {int(dur % 60)}s" if isinstance(dur, (int, float)) else "—"
    cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "—"
    return dur_s, cost_s


def list_calls(conn):
    """All voice calls, newest first, for the review list."""
    rows = conn.execute(
        "SELECT e.event_id, e.invoice_id, e.timestamp, e.voice_promise_captured, e.observed, "
        "       cl.name AS client_name, cl.tier "
        "FROM event_log e "
        "JOIN invoices c ON c.invoice_id = e.invoice_id "
        "JOIN clients cl ON cl.client_id = c.client_id "
        "WHERE e.action_taken='voice_call' ORDER BY e.event_id DESC").fetchall()
    out = []
    for r in rows:
        o = json.loads(r["observed"] or "{}")
        pd = _promise_date(r["voice_promise_captured"])
        dur_s, cost_s = _duration_cost(o)
        reg = conn.execute(
            "SELECT register FROM event_log WHERE invoice_id=? AND stage='decide' "
            "AND decision='voice_call' ORDER BY event_id DESC LIMIT 1", (r["invoice_id"],)).fetchone()
        tr = None
        out.append({
            "id": r["event_id"], "invoice_id": r["invoice_id"], "client": r["client_name"],
            "tier": r["tier"], "when": (r["timestamp"] or "").replace("T", " ")[:16],
            "duration": dur_s, "cost": cost_s,
            "register": (reg["register"] if reg else "—"),
            "outcome": _outcome(pd, o.get("_")), "promise_date": fmt_date(pd),
        })
    return out


def call_detail(conn, event_id):
    ev = conn.execute("SELECT * FROM event_log WHERE event_id=? AND action_taken='voice_call'",
                      (event_id,)).fetchone()
    if not ev:
        return None
    invoice_id = ev["invoice_id"]
    inv = _load(conn, invoice_id)
    o = json.loads(ev["observed"] or "{}")
    transcript = ev["voice_transcript"] or ""
    pd = _promise_date(ev["voice_promise_captured"])
    dur_s, cost_s = _duration_cost(o)

    vd = conn.execute("SELECT rule_applied, register, register_reason FROM event_log "
                      "WHERE invoice_id=? AND stage='decide' AND decision='voice_call' "
                      "ORDER BY event_id DESC LIMIT 1", (invoice_id,)).fetchone()
    if vd:
        rule = re.sub(r"^override \d+:\s*", "", vd["rule_applied"] or "").replace("->", "→").strip()
        register, reg_reason = vd["register"], vd["register_reason"]
    else:
        rule, register, reg_reason = "manual override", inv["tier"], "founder-initiated call"

    # brief that was sent (stored on the placed/override event, else rebuild)
    placed = conn.execute("SELECT message_sent FROM event_log WHERE invoice_id=? AND action_taken IN "
                          "('voice_call_placed','voice_call_manual_override') AND message_sent IS NOT NULL "
                          "ORDER BY event_id DESC LIMIT 1", (invoice_id,)).fetchone()
    brief = placed["message_sent"] if placed else build_vapi_prompt(invoice_id)

    diagnosis = _latest_diagnosis(conn, invoice_id)
    cap = CONTACT_CAP["strategic" if inv["segment"] == "strategic" else "standard"]
    post = decide(inv, diagnosis, cap, voice_done=True)

    return {
        "id": event_id, "invoice_id": invoice_id, "client": inv["client_name"],
        "tier": inv["tier"], "amount": inv["amount_inr"],
        "days_past_terms": inv["days_past_terms"], "duration": dur_s, "cost": cost_s,
        "rule": rule, "register": register, "register_reason": reg_reason,
        "brief": brief, "turns": parse_turns(transcript),
        "promise_date": fmt_date(pd), "extracted_raw": ev["voice_promise_captured"],
        "outcome": _outcome(pd, transcript),
        "next": post, "review": review(transcript, pd, inv["amount_inr"]),
    }


def build_invoice_pdf(ctx):
    """One-page PDF of the invoice trail: header, stages+evidence+reasons, routing."""
    from fpdf import FPDF
    inv, client = ctx["inv"], ctx["client"]
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()

    def line(txt, size=10, style="", h=5, ind=0):
        pdf.set_font("Helvetica", style, size)
        pdf.set_x(15 + ind)
        pdf.multi_cell(180 - ind, h, txt.encode("latin-1", "replace").decode("latin-1"))

    line(f"Invoice {inv['invoice_id']} - {client['name']}", 15, "B", 7)
    line(f"{client['tier'].upper()} tier   Amount Rs {inv['amount_inr']:,}   "
         f"Net {inv['terms_days']}   "
         f"{inv['days_past_terms']} days past terms" if inv["days_past_terms"] > 0
         else f"{client['tier'].upper()} tier   Amount Rs {inv['amount_inr']:,}   "
              f"Net {inv['terms_days']}   inside terms", 10)
    pdf.ln(3)

    for s in ctx["stages"]:
        line(s["heading"], 11, "B", 6)
        for b in s.get("blocks", []):
            if b["kind"] in ("bullets", "evidence"):
                for it in b.get("lines", []):
                    line(f"- {it}", 9, "", 5, ind=4)
            elif b["kind"] == "rule":
                line(b["text"], 9, "I", 5, ind=4)
            elif b["kind"] == "tone":
                line(f"Register: {b['register']} - {b.get('reason','')}", 9, "I", 5, ind=4)
            elif b["kind"] == "handover":
                line(f"Routing: {b.get('reason','')}", 9, "", 5, ind=4)
                for k, v in b.get("rows", []):
                    line(f"  {k}: {v}", 9, "", 5, ind=8)
        pdf.ln(1)

    out = pdf.output()
    return bytes(out)
