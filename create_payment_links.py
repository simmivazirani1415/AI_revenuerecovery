"""Create Razorpay test-mode payment links for actionable invoices.

For every actionable (not fully paid) invoice that does not yet have a link,
create a test-mode payment link with notify and reminder_enable both OFF,
then write rzp_link_id and rzp_link_url back to the invoices table.

Safe to re-run: invoices that already carry a link are skipped, and each
link is committed the moment it's created, so an interrupted run resumes
cleanly. Links are created for the OUTSTANDING balance, so a partially paid
invoice gets a link for what's still owed.
"""

import os
import sqlite3
import time

import razorpay
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "agency.db")

RATE_LIMIT_SECONDS = 1.2       # gentle pause between API calls
MAX_RETRIES = 5                # retries on "Too many requests"
MAX_TEST_AMOUNT_INR = 500000   # Razorpay test-mode payment-link ceiling


def create_link_with_retry(client, payload):
    """Create a link, backing off on rate-limit errors. Other errors raise."""
    delay = 2.0
    last_exc = None
    for _ in range(MAX_RETRIES):
        try:
            return client.payment_link.create(payload)
        except Exception as exc:
            last_exc = exc
            if "too many" in str(exc).lower():
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise last_exc


def ensure_columns(conn):
    """Add the rzp_ columns if this DB predates them (defensive)."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(invoices)")}
    if "rzp_link_id" not in have:
        conn.execute("ALTER TABLE invoices ADD COLUMN rzp_link_id TEXT")
    if "rzp_link_url" not in have:
        conn.execute("ALTER TABLE invoices ADD COLUMN rzp_link_url TEXT")
    conn.commit()


def main():
    load_dotenv()
    key_id = os.environ["RZP_KEY_ID"]
    key_secret = os.environ["RZP_KEY_SECRET"]
    client = razorpay.Client(auth=(key_id, key_secret))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_columns(conn)

    # Actionable = not fully paid. Skip anything that already has a link.
    rows = conn.execute(
        """
        SELECT i.invoice_id, i.amount_inr, i.amount_paid_inr,
               i.rzp_link_id, c.name AS client_name
        FROM invoices i
        JOIN clients c ON c.client_id = i.client_id
        WHERE i.status != 'paid'
        ORDER BY i.invoice_id
        """
    ).fetchall()

    actionable = len(rows)
    todo = [r for r in rows if not r["rzp_link_id"]]
    skipped = actionable - len(todo)

    created = 0
    failed = 0
    over_cap = 0

    for i, r in enumerate(todo):
        inv_id = r["invoice_id"]
        outstanding = r["amount_inr"] - r["amount_paid_inr"]

        # Razorpay test mode won't mint links above its ceiling — skip cleanly
        # (these succeed on an activated live account with a higher limit).
        if outstanding > MAX_TEST_AMOUNT_INR:
            over_cap += 1
            print(f"  SKIP  {inv_id}  Rs {outstanding:>9,}  over test-mode cap")
            continue

        try:
            link = create_link_with_retry(client, {
                "amount": outstanding * 100,      # paise
                "currency": "INR",
                "reference_id": inv_id,           # unique per invoice
                "description": f"{r['client_name']} - {inv_id}",
                "notify": {"sms": False, "email": False},
                "reminder_enable": False,
            })
        except Exception as exc:  # do not send a broken link; keep going
            failed += 1
            print(f"  FAIL  {inv_id}: {exc}")
        else:
            # Persist immediately so a re-run never double-creates.
            conn.execute(
                "UPDATE invoices SET rzp_link_id = ?, rzp_link_url = ? "
                "WHERE invoice_id = ?",
                (link["id"], link["short_url"], inv_id),
            )
            conn.commit()
            created += 1
            print(f"  OK    {inv_id}  Rs {outstanding:>9,}  {link['short_url']}")

        if i < len(todo) - 1:
            time.sleep(RATE_LIMIT_SECONDS)

    conn.close()

    print("\nSummary")
    print(f"  actionable invoices : {actionable}")
    print(f"  already had a link  : {skipped}")
    print(f"  created this run    : {created}")
    print(f"  over test-mode cap  : {over_cap}")
    print(f"  failed              : {failed}")


if __name__ == "__main__":
    main()
