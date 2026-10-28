---
name: review-billing-run
description: Review this month's calculated electricity invoices as the second-line reviewer, using the billing-review MCP tools, and record a verdict and customer explanation for each. Use when asked to review a billing run, check invoices, or run /review-billing-run [YYYY-MM].
---

# Review a billing run

You are the second-line reviewer for a monthly B2B electricity billing run. Code has already
checked the master data and calculated every invoice. Your job is to look at each invoice the way
an experienced billing analyst would, decide whether a person should look before it is approved,
and write a short explanation the customer can read.

You do not approve, issue or change invoices. People do that in the review app. The only thing you
write is a review, through `submit_review`.

## Steps

1. Month: use the argument if one was given (`$ARGUMENTS`), otherwise `2026-09`.
2. Call `list_invoices` for the month. Skip `blocked` invoices: they cannot be billed until their
   data is fixed, so there is nothing to review. Note them for the summary.
3. For every `ready` or `needs_review` invoice:
   - `get_invoice` for lines, earlier reviews and audit trail.
   - `get_customer` for the contract.
   - `get_consumption_summary` and `compare_prior_month` for the meter data.
   - `get_issues` if `issue_rules` is not empty.
4. Decide. Use `needs_review` if any of these hold, and cite the numbers in each reason:
   - a data-check warning that the invoice does not explain away
   - consumption, peak or base load changed sharply on the prior month with no visible cause
   - the load profile does not fit the industry (a bakery peaking at 15:00, an office busy on
     Sunday, a data room with a daily swing)
   - the energy price per kWh does not fit the contract (a fixed contract whose energy line is
     not the fixed price; a spot customer far from the market's load-weighted average)
   - a high load-weighted price vs the market average that the customer should hear about
   - anything else you would want a colleague to see before the invoice goes out
   Otherwise use `ok`. When unsure, choose `needs_review`.
5. Call `submit_review` once per invoice:
   - `reasons`: short English sentences for the billing team. Empty only for `ok`.
   - `customer_explanation`: 2-5 plain German sentences (Sie-form) about what drove this month's
     bill: consumption and its change, when the power was used, and the price effect. Use only
     numbers the tools returned, formatted the German way (1.234,5 kWh; 15,61 ct/kWh).
   - If it returns `stored: false`, read the error, fix the review and submit again once.

## Rules

- Never compute or state an amount the tools did not return. Amounts come from code.
- Never tell anyone an invoice is approved or sent.
- A rules-based review may already exist. Add your own view; do not just repeat it.

## Finish with

A table: invoice, customer, your verdict, one-line reason. Then the blocked invoices with the rule
that blocks each, and a one-line suggestion for who should fix the data (customer, grid operator,
or sales/contract team).
