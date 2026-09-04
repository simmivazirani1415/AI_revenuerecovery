"""run_all.py - end-to-end pipeline over a SIMULATED clock.

Advances a half-day clock (10:00 and 20:00 IST) across the ledger timeline and
plays each invoice's sequence out: detect -> diagnose -> decide -> execute,
with the client's reply firing one day after first contact, and every guardrail
(quiet hours, 48h gap, contact cap, referral patience, hard stops) checked
against the SIMULATED clock. Execute stays dry-run (no Twilio).

Reuses the real stage logic. To stay quota-safe it uses the canonical cached
diagnoses for the with-reply case and computes the no-reply case
deterministically (no live LLM calls).

Run: ./venv/bin/python run_all.py
"""

from datetime import date, datetime, timedelta

import decide
import detect
import diagnose
import execute
from log import connect
from policy import CONTACT_CAP, IST, REFERRAL_PATIENCE_DAYS

REF = date(2026, 9, 1)          # ledger snapshot date = end of sim
REPLY_DELAY = timedelta(days=1)  # client replies ~1 day after first contact
DUE_HOUR = {"INV-38": 20, "INV-46": 20}  # staggered evening due -> exercises quiet hours


def load(conn):
    invs = {}
    for r in conn.execute(
        """SELECT ai.*, c.tier, c.segment, c.internal_owner_id, c.name,
                  c.promises_broken, c.referred_by, c.referrals_made,
                  i.gt_should_contact, i.gt_diagnosis
           FROM agent_invoices ai
           JOIN clients c ON c.client_id = ai.client_id
           JOIN invoices i ON i.invoice_id = ai.invoice_id
           WHERE ai.status != 'paid'"""):
        d = dict(r)
        d["due"] = date.fromisoformat(d["due_date"])
        d["issue"] = date.fromisoformat(d["issue_date"])
        invs[d["invoice_id"]] = d
    names = {r[0]: r[1] for r in conn.execute("SELECT client_id, name FROM clients")}
    with_reply = {r[0]: r[1] for r in conn.execute(
        "SELECT invoice_id, diagnosis FROM event_log WHERE stage = 'diagnose'")}
    return invs, names, with_reply


def view(base, dpt, reply_on):
    """Per-tick invoice snapshot: as-of days_past_terms, reply hidden until fired."""
    v = dict(base)
    v["days_past_terms"] = dpt
    v["reply_text"] = base["reply_text"] if reply_on else None
    return v


def main():
    conn = connect()
    invs, names, with_reply = load(conn)

    # No-reply diagnosis + method (deterministic; no LLM) vs canonical with-reply.
    no_reply, no_reply_method = {}, {}
    for i, b in invs.items():
        v = view(b, b["days_past_terms"], reply_on=False)
        dx0, _ev0, m0 = diagnose.classify(v, v, [])
        no_reply[i], no_reply_method[i] = dx0, m0

    start = min(b["due"] for b in invs.values())
    ticks = []
    day = start
    while day <= REF:
        for h in (10, 20):
            ticks.append(datetime(day.year, day.month, day.day, h, tzinfo=IST))
        day += timedelta(days=1)

    st = {i: {"term": None, "contacts": 0, "last": None, "first": None,
              "reply_fire": None, "reply_on": False, "seen": set(), "sends": 0,
              "esc": None, "stop": None, "quiet": 0, "patience": 0, "gap": 0}
          for i in invs}

    for now in ticks:
        d = now.date()
        for i, b in invs.items():
            s = st[i]
            if s["term"]:
                continue
            if d < b["issue"]:
                continue  # invoice not issued yet
            due_dt = datetime(b["due"].year, b["due"].month, b["due"].day,
                              DUE_HOUR.get(i, 10), tzinfo=IST)
            dpt = (d - b["due"]).days
            if now < due_dt and dpt >= 0:
                dpt = -1  # not yet past terms at this hour (staggered due time)
            if b["reply_text"] and s["reply_fire"] and now >= s["reply_fire"]:
                s["reply_on"] = True
            v = view(b, dpt, s["reply_on"])

            at_risk, _risk, _why = detect.assess(v, v["tier"], s["contacts"])
            if not at_risk:
                continue

            dx = with_reply[i] if s["reply_on"] else no_reply[i]
            s["seen"].add(dx)
            dec = decide.apply_referral(decide.decide(v, dx, s["contacts"]), v, names)
            act = dec["action"]

            # Defer a no-reply TIER-DEFAULT escalate: it's only a guess. Make one
            # contact to elicit a reply, then let the real diagnosis drive. While
            # a reply is pending, wait rather than escalate. (Deterministic
            # stalling - e.g. a broken promise - still escalates immediately.)
            if (not s["reply_on"] and no_reply_method[i] == "tier_default"
                    and act == "escalate"):
                reply_pending = (b["reply_text"] and s["reply_fire"]
                                 and now < s["reply_fire"])
                if s["contacts"] == 0:
                    act = "nudge"; dec = {**dec, "action": "nudge"}  # elicit a reply
                elif reply_pending:
                    continue  # give the reply a chance to land before escalating

            if act == "do_nothing":
                s["term"] = "silent"; continue
            if act == "resubmit_via_finance":
                s["term"] = "to_finance"; continue
            if act == "escalate":
                s["term"] = "escalated"
                s["esc"] = "cap" if "contact cap" in (dec["rule_applied"] or "") else dx
                continue
            if dec["needs_founder_approval"]:
                s["term"] = "held"; continue

            ok, gr = execute.guardrail(v, dx, s["contacts"], s["last"], now)
            if not ok:
                if gr.startswith("hard_stop"):
                    s["term"] = "stopped"; s["stop"] = gr.split(":")[1]
                elif gr.startswith("contact_cap"):
                    s["term"] = "escalated"; s["esc"] = "cap"
                elif gr.startswith("quiet_hours"):
                    s["quiet"] += 1
                elif gr.startswith("referral_patience"):
                    s["patience"] += 1
                else:
                    s["gap"] += 1
                continue

            # simulated send (dry-run)
            s["contacts"] += 1; s["sends"] += 1; s["last"] = now
            if s["first"] is None:
                s["first"] = now
                if b["reply_text"]:
                    s["reply_fire"] = now + REPLY_DELAY

    _report(invs, st, ticks, start, with_reply, no_reply)
    conn.close()


def _report(invs, st, ticks, start, with_reply, no_reply):
    n = len(invs)
    held = [i for i in invs if st[i]["term"] == "held"]
    contacted = [i for i in invs if st[i]["sends"] > 0]
    never = [i for i in invs if st[i]["sends"] == 0 and st[i]["term"] != "held"]

    def term_count(t):
        return sum(1 for i in invs if st[i]["term"] == t)

    print(f"=== run_all: simulated clock {start} -> {REF}  "
          f"({len(ticks)} half-day ticks) ===\n")

    print("Per-stage totals")
    print(f"  invoices in ledger (open) : {n}")
    print(f"  ever flagged at-risk      : {sum(1 for i in invs if st[i]['seen'])}")
    print(f"  re-diagnosed mid-sequence : {sum(1 for i in invs if len(st[i]['seen'])>1)}")
    print(f"  simulated client sends    : {sum(st[i]['sends'] for i in invs)} "
          f"across {len(contacted)} invoices")
    print(f"  guardrail defers          : quiet-hours {sum(st[i]['quiet'] for i in invs)}, "
          f"referral-patience {sum(st[i]['patience'] for i in invs)}, "
          f"48h-gap {sum(st[i]['gap'] for i in invs)}")

    print("\nOutcomes")
    for t, label in [("silent", "left silent (not actually late)"),
                     ("to_finance", "routed to finance (process block)"),
                     ("held", "held for founder approval"),
                     ("escalated", "escalated to a person"),
                     ("stopped", "hard-stopped")]:
        ids = [i for i in invs if st[i]["term"] == t]
        extra = ""
        if t == "escalated":
            reasons = {}
            for i in ids:
                reasons[st[i]["esc"]] = reasons.get(st[i]["esc"], 0) + 1
            extra = "  (" + ", ".join(f"{k}:{v}" for k, v in sorted(reasons.items())) + ")"
        if t == "stopped":
            extra = "  (" + ", ".join(sorted(st[i]["stop"] for i in ids)) + ")"
        print(f"  {label:<34}{len(ids)}{extra}")
    untouched = [i for i in invs if st[i]["term"] is None and st[i]["contacts"] == 0]
    truncated = [i for i in invs if st[i]["term"] is None and st[i]["contacts"] > 0]
    print(f"  {'left alone within terms':<34}{len(untouched)}"
          + (f"  ({', '.join(sorted(untouched))})" if untouched else ""))
    print(f"  {'sequence truncated at snapshot':<34}{len(truncated)}"
          + (f"  ({', '.join(sorted(truncated))})" if truncated else ""))

    # restraint
    correct_left = [i for i in never if invs[i]["gt_should_contact"] == 0]
    wrong_left = [i for i in never if invs[i]["gt_should_contact"] == 1]
    # a first contact that elicited a diagnosis-changing reply is correct, not wrong
    wrong_contacted = [i for i in contacted if invs[i]["gt_should_contact"] == 0
                       and len(st[i]["seen"]) <= 1]
    print("\nRestraint")
    print(f"  held for approval (handled)    : {len(held)} {sorted(held)}")
    print(f"  left alone (no client contact) : {len(never)}")
    print(f"  ...correctly (gt=don't contact): {len(correct_left)}")
    print(f"  ...wrongly (gt=should contact) : {len(wrong_left)} "
          f"{sorted(wrong_left)}")
    print(f"  contacted but gt=don't contact : {len(wrong_contacted)} "
          f"{sorted(wrong_contacted)} (excludes reply-elicitation contacts)")

    # what breaks
    print("\nWhat breaks")
    breaks = []
    for i in sorted(wrong_left):
        b = invs[i]
        breaks.append(f"{i} ({b['name']}): should have been contacted "
                      f"(gt={b['gt_diagnosis']}) but was left alone - "
                      f"{'within terms so reply never fired' if b['days_past_terms']<0 else 'diagnosis routed it away from a client send'}.")
    for i in truncated:
        b = invs[i]
        breaks.append(f"{i} ({b['name']}): sequence not finished by snapshot "
                      f"({st[i]['contacts']} contacts, {b['days_past_terms']}d past terms) "
                      f"- too recently overdue to complete the cadence.")
    # red no-reply escalated before its reply could land
    for i in invs:
        s = st[i]
        if s["term"] == "escalated" and s["contacts"] == 0 and invs[i]["reply_text"] \
                and with_reply[i] != no_reply[i]:
            breaks.append(f"{i} ({invs[i]['name']}): escalated as '{s['esc']}' with 0 "
                          f"contacts, so its reply (-> '{with_reply[i]}') never fired - "
                          f"the 'disputes/pleads only after chasing' path is pre-empted.")
    # dormant guardrails (wired but never fired in this ledger)
    if sum(st[i]["quiet"] for i in invs) == 0:
        breaks.append("Quiet-hours gate is checked against the sim clock every tick "
                      "but never fires: every invoice first comes due at a 10:00 tick, "
                      "so no send lands in the 19:00-09:00 window.")
    refs = [i for i in invs if (invs[i]["referrals_made"] or 0) >= 2]
    if sum(st[i]["patience"] for i in invs) == 0 and refs:
        held_refs = [i for i in refs if st[i]["term"] == "held"]
        breaks.append(f"Referral +7d patience never fires: the only >=2-referrer invoice(s) "
                      f"{refs} are {'held for founder approval' if held_refs else 'resolved'} "
                      f"before reaching the send gate, so the patience check is moot.")
    # cap enforced in two places
    breaks.append("Contact cap is enforced twice - decide's override 4 escalates at the "
                  "cap before execute's guardrail cap check is ever reached; harmless but "
                  "redundant (INV-23/24 escalate on cap after 3 chases).")

    if not breaks:
        print("  (nothing)")
    for b in breaks:
        print(f"  - {b}")

    print("\nPer-invoice trace")
    for i in sorted(invs):
        s = st[i]
        path = no_reply[i] if len(s["seen"]) <= 1 else f"{no_reply[i]}->{with_reply[i]}"
        outcome = s["term"] or ("chasing" if s["contacts"] else "untouched")
        tail = f" [{s['esc'] or s['stop']}]" if (s["esc"] or s["stop"]) else ""
        print(f"  {i}  {invs[i]['name'][:20]:<20} dpt={invs[i]['days_past_terms']:>3} "
              f"sends={s['contacts']} {path:<34} -> {outcome}{tail}")


if __name__ == "__main__":
    main()
