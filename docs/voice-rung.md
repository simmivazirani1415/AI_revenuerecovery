# Voice — rung 4 of the escalation ladder

Demonstration level, not full telephony. Voice is inserted between the text
contacts and the human handover, and it reuses the exact same decision context the
text path uses — the register and reason `decide.py` chose — just rendered for a
Vapi voice agent instead of an SMS.

## The ladder now

```
rung 1-3   text contacts (WhatsApp)         cap = 3 for standard
rung 4     voice_call (Vapi)                 if voice_permitted and not yet tried
rung 5     escalate to a human              snehal (amber) / aditya (red)
```

`decide.py` — at the contact cap, before the human handover:

```
contacts=0            -> offer_payment_plan   (unchanged; voice never fires early)
contacts=3 voice ok   -> voice_call           (override 4: cap reached -> voice call, rung 4)
contacts=3 voice done -> escalate             (after voice -> human, rung 5)
contacts=3 no voice   -> escalate             (voice not permitted -> ladder exactly as before)
```

Fires only when: the contact cap is reached, `voice_permitted` is true on the
client profile, and the invoice hasn't already voice-called or escalated. In every
other case the ladder behaves exactly as it did before.

**Schema:** `clients.voice_permitted` (0/1), and `event_log.voice_transcript` +
`event_log.voice_promise_captured`. Voice is enabled for Brightline and Kestrel.

## build_vapi_prompt — the register carries through

Same inputs as `execute.draft()` (profile, register, register_reason from
`decide.py`); different output (a spoken-agent brief). Only the tone block swings —
driven entirely by the register.

| | INV-23 · Brightline | INV-45 · Kestrel |
|---|---|---|
| Diagnosis | cash_stress | stalling |
| **Register** | **warm** — *genuine cash difficulty, don't pressure* | **firm** — *red tier, firm register* |
| Tone line | "Warm and understanding. Acknowledge the cash pressure, do not pressure… offer the instalment plan… Hinglish is fine." | "Firm and direct. Make clear this has been chased repeatedly and is now escalating internally. Offer no new concessions…" |
| History fed in | paid 3, 0 broken promises, worst 55d | paid 2, **2 broken** promises, worst 30d |
| Objective / rules | identical scaffold (commitment + date, capture the promise, stop on dispute, no threats, ≤2 min) | identical scaffold |

## ingest_transcript — a real Vapi call

Ran the actual INV-23 transcript through `ingest_transcript`. The shared
`extract_promise` (the same promise/date logic the WhatsApp path should use) pulled:

```
promised : true
date     : 2026-09-11        (client said "Friday", this month)
intent   : क्लियर            (clear/pay)
phrase   : friday
```

- **Event written:** `stage=execute, action_taken=voice_call, channel=voice,
  outcome=voice_promise_captured, voice_promise_captured="pending 2026-09-11"`, full
  transcript stored in `voice_transcript`.
- **Landed on the profile:** INV-23 → `promise_status=pending`,
  `promise_date=2026-09-11` — identical effect to capturing a promise from a text
  reply, so the rest of the pipeline (override 3 "wait it out", the broken-promise
  path) treats a voice promise exactly like a text one.

## Quality issues in this call (recorded honestly)

The extraction worked despite the noise, but the call itself had three defects.
**All three are prompt or transcriber-config fixes — none are architecture
problems:**

1. **Transcriber bled Urdu script into Hinglish.** Mid-call the transcript flips
   into Urdu script ("شکریہ… کلیئر کر دیں گے") between Devanagari lines. That's the
   speech-to-text/transcriber language config, not the bridge. (The extractor was
   made script-agnostic so it still captured the promise; the underlying render is
   still a transcriber-config fix.)

2. **The model hedged gender.** "बोल रहा हूँ", "कर लेता लेती हूँ", "दूँगा दूँगी",
   "रहा रही हूँ" — it says both masculine and feminine forms because the system
   prompt never pins the agent's persona/gender. Fix: fix a single persona (name +
   gender) in `build_vapi_prompt`.

3. **The amount rendered wrong.** The call said "3000 की पेमेंट", then "INV-23 लाख
   रुपये", then "300000 रुपये" — the real figure is ₹300,000. The number was
   improvised/garbled. Fix: inject the exact amount into the prompt and instruct the
   agent to read it verbatim and not restate it in words.

The pipeline (decide → voice brief → transcript → promise register) held up; the
gaps are in how the voice agent is prompted and how the call is transcribed.
