"""LLM provider config for reply classification — the one place to swap models.

Primary is Gemini (gemini-2.0-flash), fallback is OpenAI. To change which model
is primary, reorder PROVIDER_CHAIN — that single line is the whole switch.

classify_reply() tries each provider in order: on a rate-limit or auth error it
retries once after a short delay, then falls through to the next provider. A
1-second delay precedes every call to stay under per-minute caps. It returns
(label, provider_name), or (None, None) if no provider could answer.
"""

import json
import os
import time

from dotenv import load_dotenv

# Persist successful classifications so identical replies aren't re-billed or
# re-exposed to rate limits; makes runs converge and stay reproducible.
_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_cache.json")

DIAGNOSES = ["not_actually_late", "process_block", "approver_bottleneck",
             "wrong_contact", "dispute", "cash_stress", "stalling"]

# The classification prompt — kept verbatim; do not edit when swapping providers.
PROMPT = """You classify a B2B accounts-receivable client reply into exactly one label:
- not_actually_late: terms not breached, or a known payment freeze; nobody should act.
- process_block: PO/GST/portal/proforma/documentation problem the vendor must fix and resubmit.
- approver_bottleneck: invoice acknowledged but stuck awaiting sign-off / a person on leave / their own client paying first.
- wrong_contact: it never reached accounts payable / contact is wrong.
- dispute: milestone, scope, deliverable quality or a line item is contested.
- cash_stress: the client cites cashflow difficulty.
- stalling: engages but avoids paying, vague "next week" promises, no real blocker.
Answer with ONLY the label, nothing else."""

CALL_DELAY_SECONDS = 1.0    # spacing between API calls
RETRY_DELAY_SECONDS = 2.0   # short pause before the single retry

# One-off cache warming under strict free-tier limits: when LLM_PATIENT is set,
# a rate-limited call waits the server-suggested time and keeps retrying instead
# of falling back after one try. Production default is the specced one-retry.
_PATIENT = bool(os.getenv("LLM_PATIENT"))
_PATIENT_MAX_ATTEMPTS = 8


def _call_gemini(model, key, user):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=model, contents=user,
        config=types.GenerateContentConfig(system_instruction=PROMPT, temperature=0),
    )
    return resp.text


def _call_openai(model, key, user):
    from openai import OpenAI
    client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "system", "content": PROMPT},
                  {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content


# Provider registry. Reorder PROVIDER_CHAIN to change primary/fallback.
PROVIDERS = {
    # NOTE: requested gemini-2.0-flash is retired (API returns 404, points to 3.6);
    # using the current flash model. Change this one line to swap the model.
    "gemini": {"model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY", "call": _call_gemini},
    "openai": {"model": "gpt-4o-mini", "key_env": "OPENAI_API_KEY", "call": _call_openai},
}
PROVIDER_CHAIN = ["gemini", "openai"]   # primary first, then fallback


def _retry_seconds(exc, default):
    """Pull the server-suggested 'retry in Xs' from a 429, else use default."""
    import re
    m = re.search(r"retry in ([\d.]+)s", str(exc))
    return min(float(m.group(1)) + 2, 65) if m else default


def _is_rate_or_auth(exc):
    s = str(exc).lower()
    markers = ("429", "rate limit", "rate_limit", "quota", "resource_exhausted",
               "401", "403", "auth", "unauthenticated", "permission", "api key",
               "api_key", "invalid key",
               # google-genai renders a 429 as this and then wedges its
               # transport shut for the rest of the process; treat as rate-limit.
               "client has been closed", "has been closed")
    return any(m in s for m in markers)


def _normalize(text):
    if not text:
        return None
    low = text.strip().lower()
    for d in DIAGNOSES:
        if d in low:
            return d
    return None


def _load_cache():
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def classify_reply(reply, context):
    """Classify one reply. Returns (label, provider_name) or (None, None).

    Cached hits return the provider that originally answered, so the evidence
    string stays truthful about which model made the call.
    """
    load_dotenv()
    user = f"Context: {context}\nReply: {reply!r}"

    cache = _load_cache()
    if user in cache:
        hit = cache[user]
        return hit["label"], hit["provider"]

    for name in PROVIDER_CHAIN:
        cfg = PROVIDERS[name]
        key = os.getenv(cfg["key_env"])
        if not key:
            continue

        max_attempts = _PATIENT_MAX_ATTEMPTS if _PATIENT else 2
        for attempt in range(1, max_attempts + 1):
            time.sleep(CALL_DELAY_SECONDS)
            try:
                label = _normalize(cfg["call"](cfg["model"], key, user))
                if label:
                    cache[user] = {"label": label, "provider": name}
                    with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                        json.dump(cache, f, indent=2, ensure_ascii=False)
                    return label, name
                break  # answered but unparseable — move to next provider
            except Exception as exc:
                if _is_rate_or_auth(exc) and attempt < max_attempts:
                    time.sleep(_retry_seconds(exc, RETRY_DELAY_SECONDS)
                               if _PATIENT else RETRY_DELAY_SECONDS)
                    continue          # retry, then fall to next provider
                break                 # give up on this provider
    return None, None
