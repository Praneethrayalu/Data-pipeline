# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
Claude (a large language model, used through the Instinct assistant), plus `pytest`.

## What I used them for
The AI wrote the entire implementation in `solution/` - the normalisation rules,
ingestion and ledger logic, report builders, database schema, tests, and the first
draft of `SUBMISSION.md` - under my direction. My direction was: do this assignment,
pick whichever of A/B it judged stronger, and build it to a professional bar. I am
reviewing the code before submission; nothing here should be read as a claim that I
wrote it myself.

## What I wrote or decided myself
- The decision to proceed with this assignment and to submit it.
- Delegating the A-vs-B choice to the AI (it picked A: deterministic outputs it can
  verify end-to-end) and approving that choice.
- Review of the design decisions recorded in `SUBMISSION.md` is in progress; I will
  update this file before the interview if any statement here stops being accurate.

## Where the AI was wrong or unhelpful
- Its first draft routed the disposition catalogue through a database meta table for
  no reason; it was simplified to pass the catalogue directly after a review pass.
- Its first test fixture contained two wrong expected values (`events_total` and a
  `success_leads` count). The pipeline code was right and the test expectations were
  wrong; this was caught by reading the failing assertions against the spec rather
  than editing the code to match the tests.

## How I verified AI-generated code
The following checks were executed against this exact tree (all re-runnable from a
clean clone), and I am re-running them as part of my review:
- 34 automated tests: rule unit tests plus an end-to-end fixture covering rejections,
  duplicate merging, orphan relinking, phone-less overrides, unknown dispositions and
  malformed lines.
- The real generator on seed 42 (small and large) and an unseen seed 7.
- Byte-identical report diffs across re-ingestion and across bulk-vs-sequential
  ingestion, plus an independent recomputation of report aggregates straight from
  the raw CSV/JSONL files (not reusing pipeline code).
- A byte scan of the SQLite database confirming no plaintext PII is stored.
