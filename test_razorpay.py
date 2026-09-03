"""Smoke test: create a test-mode Razorpay payment link.

Loads credentials from .env, creates a payment link for Rs 2500
with customer notifications and reminders disabled, then prints
the resulting short_url and id.
"""

import os

import razorpay
from dotenv import load_dotenv

load_dotenv()

key_id = os.environ["RZP_KEY_ID"]
key_secret = os.environ["RZP_KEY_SECRET"]

client = razorpay.Client(auth=(key_id, key_secret))

# Amount is in the smallest currency unit — paise. Rs 2500 = 250000 paise.
payment_link = client.payment_link.create({
    "amount": 250000,
    "currency": "INR",
    "description": "Test receivable — Rs 2500",
    "notify": {
        "sms": False,
        "email": False,
    },
    "reminder_enable": False,
})

print("short_url:", payment_link["short_url"])
print("id:", payment_link["id"])
