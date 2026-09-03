"""Single source of truth for recovery policy — thresholds, caps, guardrails.

Change a value here and every stage (decide, execute) picks it up.
"""

from datetime import timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# Founder sign-off required for client-facing sends at/above this, or strategic.
FOUNDER_THRESHOLD_INR = 300000

# Max client-facing contacts per invoice, by client segment.
CONTACT_CAP = {"strategic": 2, "standard": 3}

# Guardrails
QUIET_HOURS_START = 9        # inclusive, IST
QUIET_HOURS_END = 19         # exclusive, IST (7pm)
MIN_HOURS_BETWEEN_CONTACTS = 48

# Referral policy (founder-set)
# - A client who has referred >= N others earns a warmer register and extra
#   patience before the first contact.
# - A client who was referred by someone gets a softer register and escalates
#   one step earlier, with the referrer named in the routing reason.
REFERRALS_WARMER_THRESHOLD = 2
REFERRAL_PATIENCE_DAYS = 7
