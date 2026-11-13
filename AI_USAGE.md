# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
Claude (a large language model, used through the Instinct assistant), plus `pytest`
for verification. No Copilot/Cursor.

## What I used them for
Heavily. I directed the work - chose Assignment A, set the quality bar, and decided
to submit - and the AI wrote most of the implementation: the normalisation rules,
the ingestion and ledger logic, the report builders, the database schema, and the
test suite. I used it the way I'd use a strong pair programmer: I gave it the spec,
reviewed its design decisions (PII pseudonymisation approach, drop-level
idempotency, orphan fixpoint relinking), and checked its output against the
assignment's rules rather than trusting it.

## What I wrote or decided myself
- The choice of Assignment A over B, and the decision of what "done" means against
  the output contract.
- Reviewed and signed off the key design calls in `SUBMISSION.md` (drop-level
  idempotency, HMAC pseudonymisation for PII, orphan relinking semantics).
- Read the spec's rules section against the implementation to confirm each rule
  is honoured (rejection order, phone formats, bucket definitions, report sorts).

## Where the AI was wrong or unhelpful
- Its first test fixture had two wrong expected numbers (`events_total` and a
  `success_leads` count in `daily_overview`). The pipeline code was actually
  right - the later-arriving override legitimately changes an earlier day's
  effective disposition - and I caught it by reading the failing assertion
  against the spec instead of just "fixing" the code to match the test.
- Its first draft routed the disposition catalogue through a database meta table
  for no reason; I had it simplified to pass the catalogue through directly.

## How I verified AI-generated code
- 34 automated tests: rule unit tests plus an end-to-end fixture covering
  rejections, duplicate merging, orphan relinking, overrides without phones,
  unknown dispositions, and malformed lines.
- Ran the real generator on seed 42 (small + large) and an unseen seed 7.
- Diffed report outputs byte-for-byte across re-ingestion and across
  bulk-vs-sequential ingestion.
- Byte-scanned the SQLite file to confirm no plaintext PII is stored.
