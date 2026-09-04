-- Dataset Spec v2: Agency Architecture — data layer
-- B2B Receivables Recovery Agent
--
-- Four tables (team, clients, invoices, event_log) plus one view
-- (agent_invoices) that hides the ground-truth columns. Agent code
-- reads ONLY the view; the raw invoices table is for the scoring
-- script alone. Enforced here in the schema, not by discipline.
--
-- Re-runnable: drops existing objects before recreating them.

PRAGMA foreign_keys = ON;

DROP VIEW  IF EXISTS agent_invoices;
DROP TABLE IF EXISTS event_log;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS clients;
DROP TABLE IF EXISTS team;

-- ---------------------------------------------------------------------------
-- team — who inside the agency owns each account and each problem type.
-- Escalation routes to whoever can actually act, not a generic inbox.
-- ---------------------------------------------------------------------------
CREATE TABLE team (
    person_id                TEXT PRIMARY KEY,   -- e.g. 'rhea'
    name                     TEXT NOT NULL,      -- 'Rhea Kapadia'
    role                     TEXT NOT NULL,      -- 'Finance Controller'
    service_line             TEXT,               -- 'Finance', 'V-Suite', ...
    handles_escalation_types TEXT                -- comma-separated escalation tags
);

-- ---------------------------------------------------------------------------
-- clients — identity, revenue line, commercial terms, the internal owner,
-- the AP contact, and a rolled-up view of payment behaviour / relationship.
-- `tier` is COMPUTED from history by generate_data.py, never hand-set.
-- ---------------------------------------------------------------------------
CREATE TABLE clients (
    -- identity
    client_id            TEXT PRIMARY KEY,       -- 'aureus'
    name                 TEXT NOT NULL,          -- 'Aureus Motors'
    industry             TEXT,                   -- 'automotive'

    -- classification
    revenue_line         TEXT NOT NULL,          -- project | product_licence | retainer
    segment              TEXT NOT NULL,          -- standard | strategic
    payment_terms_days   INTEGER NOT NULL,       -- net terms (varies by line)

    -- ownership & routing
    internal_owner_id    TEXT REFERENCES team(person_id),

    -- AP contact
    ap_contact_name      TEXT,
    ap_contact_verified  INTEGER NOT NULL DEFAULT 1,   -- 0/1
    client_finance_portal TEXT,                        -- 'Coupa', 'SAP Ariba', NULL

    -- relationship
    relationship_years   REAL,
    total_revenue_inr    INTEGER,

    -- referral graph
    referred_by          TEXT REFERENCES clients(client_id),   -- who referred this client
    referrals_made       INTEGER NOT NULL DEFAULT 0,           -- how many they've referred

    -- payment behaviour (rolled up from paid invoices)
    invoices_paid        INTEGER NOT NULL DEFAULT 0,
    invoices_paid_late   INTEGER NOT NULL DEFAULT 0,
    avg_days_to_pay      REAL,
    worst_days_late      INTEGER NOT NULL DEFAULT 0,   -- days past terms, paid history only

    -- interaction
    response_rate        REAL,                          -- 0..1
    engages_off_topic    INTEGER NOT NULL DEFAULT 0,    -- 0/1

    -- promises
    promises_made        INTEGER NOT NULL DEFAULT 0,
    promises_kept        INTEGER NOT NULL DEFAULT 0,
    promises_broken      INTEGER NOT NULL DEFAULT 0,

    -- derived standing
    tier                 TEXT NOT NULL             -- green | amber | red (computed)
);

-- ---------------------------------------------------------------------------
-- invoices — the full ledger. Observable operational fields PLUS four
-- gt_ ground-truth columns that the scoring script alone may read.
-- ---------------------------------------------------------------------------
CREATE TABLE invoices (
    invoice_id        TEXT PRIMARY KEY,        -- 'INV-04' (internal row id)
    invoice_number    TEXT,                    -- external number; may duplicate (edge case)
    client_id         TEXT NOT NULL REFERENCES clients(client_id),
    revenue_line      TEXT NOT NULL,

    amount_inr        INTEGER NOT NULL,        -- whole rupees
    currency          TEXT NOT NULL DEFAULT 'INR',

    issue_date        TEXT NOT NULL,           -- ISO date
    terms_days        INTEGER NOT NULL,        -- net terms for THIS invoice
    due_date          TEXT NOT NULL,           -- issue_date + terms_days

    status            TEXT NOT NULL,           -- open | partially_paid | paid
    paid_date         TEXT,                    -- when fully paid (else NULL)
    amount_paid_inr   INTEGER NOT NULL DEFAULT 0,
    days_past_terms   INTEGER,                 -- as of reference date; negative = inside terms

    -- process / signal fields (observable — agent may read these)
    po_number         TEXT,
    po_matched        INTEGER,                 -- 0/1/NULL
    milestone_ref     TEXT,
    contact_verified  INTEGER NOT NULL DEFAULT 1,
    payment_failed    INTEGER NOT NULL DEFAULT 0,   -- structural failure: auto-debit/renewal
    reply_text        TEXT,                    -- latest client reply, if any
    promise_date      TEXT,                    -- date client promised to pay
    promise_status    TEXT,                    -- kept | broken | pending | NULL

    routed_to         TEXT REFERENCES team(person_id),   -- current operational routing

    -- Razorpay test-mode payment link (populated by create_payment_links.py)
    rzp_link_id       TEXT,
    rzp_link_url      TEXT,

    -- ground truth — scoring script ONLY. Never exposed via agent_invoices.
    gt_diagnosis      TEXT,
    gt_correct_action TEXT,
    gt_should_contact INTEGER,                 -- 0/1
    gt_route_to       TEXT
);

-- ---------------------------------------------------------------------------
-- event_log — the agent's audit trail, one row per stage.
-- DENORMALIZED ON PURPOSE: each row carries the full context of the decision
-- at that moment (what was seen, concluded, decided, done, and why), so any
-- single row reconstructs the entire decision without joining another table.
-- JSON-ish text columns (observed, profile_delta) hold structured snapshots.
-- ---------------------------------------------------------------------------
CREATE TABLE event_log (
    event_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp          TEXT NOT NULL,          -- ISO-8601, when the event was logged

    invoice_id         TEXT NOT NULL REFERENCES invoices(invoice_id),
    client_id          TEXT NOT NULL REFERENCES clients(client_id),

    stage              TEXT NOT NULL           -- pipeline stage
        CHECK (stage IN ('detect', 'diagnose', 'decide', 'execute', 'escalate')),

    -- detect: full snapshot of what the agent observed (JSON)
    observed           TEXT,

    -- diagnose: the conclusion and the evidence behind it
    diagnosis          TEXT,
    diagnosis_evidence TEXT,

    -- decide: what was chosen, under which rule, and the tone taken
    decision           TEXT,
    rule_applied       TEXT,
    register           TEXT,                   -- warm | firm | final | silent ...
    register_reason    TEXT,

    -- execute: what actually happened on the wire
    action_taken       TEXT,
    message_sent       TEXT,
    channel            TEXT,                   -- whatsapp | none | internal ...

    -- outcome of this event
    outcome            TEXT,
    outcome_at         TEXT,

    -- routing (escalate / decide)
    routed_to          TEXT,                   -- person_id, denormalized name is fine too
    routed_reason      TEXT,

    -- how this event changed the client's standing (JSON: tier/register/promises)
    profile_delta      TEXT
);

-- ---------------------------------------------------------------------------
-- agent_invoices — the ONLY invoice surface agent code may read.
-- Every column of invoices EXCEPT the four gt_ ground-truth columns.
-- ---------------------------------------------------------------------------
CREATE VIEW agent_invoices AS
SELECT
    invoice_id,
    invoice_number,
    client_id,
    revenue_line,
    amount_inr,
    currency,
    issue_date,
    terms_days,
    due_date,
    status,
    paid_date,
    amount_paid_inr,
    days_past_terms,
    po_number,
    po_matched,
    milestone_ref,
    contact_verified,
    payment_failed,
    reply_text,
    promise_date,
    promise_status,
    routed_to,
    rzp_link_id,
    rzp_link_url
FROM invoices;
