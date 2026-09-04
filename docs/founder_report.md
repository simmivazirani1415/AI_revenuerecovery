# Founder Report

_Generated 2026-09-04 15:28 IST. Source: event_log + invoices (no LLM)._

## 1. Money at risk (current open receivables)

**By tier**

| tier | outstanding | inside terms | genuinely late |
|---|--:|--:|--:|
| red | Rs 3,435,000 | Rs 0 | Rs 3,435,000 |
| amber | Rs 4,295,000 | Rs 1,300,000 | Rs 2,995,000 |
| green | Rs 3,650,000 | Rs 2,980,000 | Rs 670,000 |
| **total** | **Rs 11,380,000** | **Rs 4,280,000** | **Rs 7,100,000** |

**By revenue_line**

| revenue_line | outstanding | inside terms | genuinely late |
|---|--:|--:|--:|
| product_licence | Rs 860,000 | Rs 230,000 | Rs 630,000 |
| project | Rs 9,460,000 | Rs 3,810,000 | Rs 5,650,000 |
| retainer | Rs 1,060,000 | Rs 240,000 | Rs 820,000 |
| **total** | **Rs 11,380,000** | **Rs 4,280,000** | **Rs 7,100,000** |

## 2. Agent activity

| metric | count |
|---|--:|
| invoices considered | 24 |
| acted on (sent / routed to finance) | 8 |
| deliberately silent (not actually late) | 5 |
| escalated to a human | 8 |
| held for founder approval | 3 |

## 3. Discounts & waivers applied

| invoice | client | concession | amount | authorised by | rule fired |
|---|---|---|--:|---|---|
| INV-23 | Brightline Retail | instalment plan | Rs 300,000 | account owner | table: cash_stress -> offer payment plan |
| INV-24 | Brightline Retail | instalment plan | Rs 280,000 | account owner | table: cash_stress -> offer payment plan |
| INV-56 | Vantage Consumer Group | late-fee waiver | Rs 240,000 | founder (strategic) | override 2: not_actually_late -> do nothing |

## 4. Escalations

| invoice | client | why (diagnosis) | routed to | gt match |
|---|---|---|---|:--:|
| INV-11 | Sable Properties | stalling | Snehal Rao | NO |
| INV-19 | Halcyon Pharma | dispute | Snehal Rao | yes |
| INV-33 | Trellis Infra | stalling | Aditya Menon | yes |
| INV-34 | Trellis Infra | dispute | Karan Bhatt | yes |
| INV-45 | Kestrel Events | stalling | Aditya Menon | yes |
| INV-58 | Saffron Media House | stalling | Aditya Menon | yes |
| INV-60 | Verma & Sons Developers | cash_stress | Aditya Menon | yes |
| INV-61 | Verma & Sons Developers | stalling | Aditya Menon | yes |

7/8 escalations match gt_correct_action.

## 5. Worst payers this quarter

| client | tier | promises (made/kept/broken) | avg days past terms | outstanding |
|---|---|:--:|--:|--:|
| Trellis Infra | red | 3/0/3 | 53d | Rs 2,550,000 |
| Amrit Global | red | 2/0/2 | 88d | Rs 190,000 |
| Verma & Sons Developers | red | 2/0/2 | 52d | Rs 430,000 |
| Kestrel Events | red | 2/0/2 | 42d | Rs 265,000 |
| Saffron Media House | amber | 1/0/1 | 35d | Rs 150,000 |
| Corvid Sports Network | amber | 1/0/1 | 22d | Rs 2,200,000 |

## 6. Tier changes since last run

_No tier changes since 2026-09-04T00:22._

## 7. Counterfactual: naive reminder tool

_Naive tool = no per-client terms (chases from issue date), fixed 7-day cadence, one firm template, no memory of diagnosis / promises / replies / contact status. Same open ledger._

**Disposition of the 24 open invoices**

| how it's handled | naive tool | the agent |
|---|--:|--:|
| auto-sent to client | 24 | 3 |
| held for founder approval | 0 | 3 |
| routed to a person (escalated) | 0 | 8 |
| sent to finance | 0 | 4 |
| correct silence | 0 | 5 |
| blocked at guardrail | 0 | 1 |
| **total messages fired at clients** (7-day cadence) | **188** | **3** |

**Client-facing harm** (of messages that actually reach a client)

| harm | naive tool | the agent |
|---|--:|--:|
| ...to clients **inside terms** | 8 | 0 |
| ...to clients **in dispute** | 2 | 0 |
| ...to **strategic** clients at **firm** register | 3 | 0 |
| ...to a **dead contact** | 1 | 0 |

**Sensitivity — naive tool assuming flat net-30 instead of due-on-issue**

| metric | naive net-0 | naive net-30 |
|---|--:|--:|
| invoices contacted | 24 | 15 |
| messages fired (cadence) | 188 | 92 |
| inside terms | 8 | 0 |
| in dispute | 2 | 1 |
| strategic at firm | 3 | 1 |
| dead contact | 1 | 1 |

Even the gentler net-30 naive tool still fires 92 messages and hits 1 dispute, 1 strategic account and 1 dead contact - the terms-blindness harm falls to 0, but the memory-blindness harms remain. The agent: 3 auto-sends, zero harm in any category.
