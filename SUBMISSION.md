# Submission

## Candidate
- **Name:** Praneeth Rayalu Repalle
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** the implementation was AI-assisted and completed in under an hour of elapsed time; my own review of the code is not yet completed (see `AI_USAGE.md`)

## How to run
```bash
# from a clean clone, Python 3.11+
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python solution/pipeline.py ingest data/drop_1 --db ledger.db
python solution/pipeline.py ingest data/drop_2 --db ledger.db
python solution/pipeline.py report --out reports/ --db ledger.db

# tests (only dependency: pytest)
pip install -r solution/requirements.txt
python -m pytest solution/tests -q
```
`ingest data` (the parent) also works and processes `drop_*` in name order. The
pipeline is standard-library only; SQLite is the database, no server needed.

## Design
The pipeline is a small package (`solution/ledger/`) with a strict split between
**rules**, **storage** and **derivation**:

- `normalise.py` holds every pure normalisation rule from the spec (headers,
  phones, dates, decimals, enums, timestamps) as functions that return a value
  or `None`. Nothing else in the codebase knows these rules.
- `ingest.py` applies the policy around those rules: header mapping, schema-order
  field walk with first-failure rejection, duplicate merging, event dedupe,
  orphan quarantine and relinking.
- `db.py` owns the SQLite schema and PII pseudonymisation.
- `report.py` derives everything from the ledger at report time, so reports
  always reflect *everything ingested so far* regardless of arrival order.

Database schema (SQLite):

- `leads(id, workflow_id, phone_hmac, phone_last4, name_hmac, batch_id,
  external_ref, attrs)` — one row per `(workflow_id, canonical phone)`. PII is
  never stored in plaintext (see below). Non-PII optional fields live in an
  `attrs` JSON column so merged duplicates can overwrite field-by-field.
- `ledger(id, event_id UNIQUE, event_type, workflow_id, call_id, occurred_at,
  lead_id, payload)` — the append-only interaction ledger. Events are never
  updated or deleted; overrides are just new events.
- `orphans(...)` — quarantined events with their parsed fields and raw line,
  re-evaluated after every new drop.
- `seen_events(event_id)`, `ingested_drops(drop_path)`, `rejections(...)`,
  `meta(key, value)` counters — the machinery that makes re-ingestion a no-op
  and `data_quality.json` exact.

**Idempotency:** drops are the unit of idempotency. An already-ingested drop is
skipped entirely, so re-running `ingest` changes nothing — not even the
duplicate-event counters. Within a drop, `event_id` dedupe and
`INSERT OR IGNORE` on rejections make every write convergent.

**Events that arrive before their lead** sit in `orphans` with their raw line.
After each new drop, a fixpoint loop retries linking (an override can only link
once another event of its call has linked, so one pass is not enough). Linked
events move into the ledger; `orphan_events` in `data_quality.json` is the live
count of whatever is still quarantined, i.e. the final state.

**PII:** fields marked `"pii": true` (name, phone) are stored only as
HMAC-SHA256 pseudonyms plus the phone's last four digits for masking. Matching
events to leads still works because the HMAC is deterministic. The key comes
from `AU_PII_KEY`, with a documented development default so a clean clone runs
with zero setup.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Keyed hashing (HMAC-SHA256) for PII instead of reversible encryption | No report needs to recover a name or phone; data minimisation beats encryption-with-a-key-stored-nearby | Fernet/AES with key management; rejected as more machinery for no reporting benefit |
| Drop-level idempotency (skip already-ingested drops) | "Re-running ingest on a drop you already ingested must change nothing" includes `data_quality.json` counters | Re-processing and counting everything as duplicates; rejected because counters would drift |
| Rejected = row stops at first failure in schema order | Spec | Collecting all errors per row; spec says single reason |
| Required non-phone field that fails parsing counts as `missing_required:<field>` | Spec only names `missing_required:*` and `invalid_phone`; the schemas only require strings and phones, so this path is theoretical | A new `invalid_<type>` reason; rejected as inventing contract surface |
| Unparseable event timestamp counts as malformed | The ledger is useless without a time; spec's malformed definition is "not valid JSON / no event_id" and this is the closest bucket | Quarantining as orphan; rejected because it would pollute orphan counts |
| Overrides beat sets regardless of timestamps; among same type, latest `occurred_at` wins | Spec: "latest override... if it has no override, the latest set" | Timestamp-only ordering; spec is explicit |
| Tie-break same-timestamp disposition events by ledger insertion id | Generator can emit equal timestamps; deterministic output matters for diffs | Random/order-of-read; rejected as non-deterministic |
| `success_leads` in `daily_overview` counts a lead on a date when a call *on that date* has a success effective disposition | Read of the contract: "at least one call **on that date** whose effective disposition has `is_success=true`"; effective disposition is evaluated over everything ingested so far, so a later override can make an earlier date's call successful | Only counting same-day dispositions; rejected — the ledger is the source of truth and overrides deliberately arrive later |
| `lead_rows_total` counts non-blank data rows (accepted + rejected + merged duplicates) | Blank rows are "skipped and not counted"; every other row is a lead row | Counting only accepted leads; rejected — rejected rows are still lead rows |
| Calls group by `call_id`; a call's lead/date come from its `call.initiated` event | Spec: "a call is counted only if its `call.initiated` event is present (and linked)" | Trusting per-event phones for grouping; rejected — overrides have no phone |
| Decimal rounding half-up to 2 places | "Round to 2 places"; half-up matches financial convention | Banker's rounding; no signal in spec, noted here |
| `docker-compose` Postgres not used | SQLite satisfies the contract, keeps a clean clone runnable with zero setup, and handles the large scale in 42s | Postgres; available if production-shaped concurrency mattered |

## Failure modes considered
- **Re-runs / double ingestion:** drop skip + `event_id` dedupe + idempotent
  rejection inserts; verified by test (`test_reingest_changes_nothing`).
- **Events before leads:** orphan quarantine + fixpoint relinking
  (`test_orphan_counts_before_lead_arrives`).
- **Duplicate events within and across drops:** `seen_events` is global.
- **Overrides with no phone:** linked through `call_id` to the call's other
  events; stay quarantined until one links.
- **Malformed lines / missing event_id / bad timestamps:** counted, skipped.
- **Unknown disposition codes:** counted, never enter the ledger.
- **Dirty lead rows:** blank-row skip, header alias mapping, renamed/re-ordered
  columns (batch_003 uses aliases), extra columns ignored, every documented
  phone/date/decimal/enum mess.
- **Crash mid-drop:** a drop is only recorded in `ingested_drops` after it
  completes; a crash means the drop is re-ingested and converges via
  `event_id` dedupe and rejection `INSERT OR IGNORE`. Counters could
  double-count if a crash lands mid-event-file after a commit — a production
  system would make each drop one transaction; noted as a known limit.
- **Not handled:** concurrent ingest processes on one database; workflow IDs
  in event files with no schema (they orphan); leads whose phone changes.

## Testing
34 tests (`python -m pytest solution/tests -q`, all passing):
- Unit tests for phone normalisation (all six generator formats + the invalid
  ones), all four date formats plus impossible dates, decimal prefixes/commas/
  negatives/rounding, enum case handling, header normalisation, all three
  timestamp forms including the IST/UTC date boundary.
- Unit tests for effective disposition (override beats later set; latest
  override wins) and bucket logic (all four buckets, DND terminal).
- An end-to-end fixture exercising rejections and reasons, blank rows, alias
  headers, duplicate merging across drops, orphans that link in a later drop,
  phone-less overrides, unknown dispositions, duplicate and malformed lines,
  re-ingest idempotency, bulk-vs-sequential equivalence, and a byte scan of
  the database proving no plaintext PII.
- Verified on the real generator: seed 42 small, seed 7 small (unseen seed),
  and seed 42 large. Re-ingesting both drops produces byte-identical reports;
  ingesting the parent directory matches sequential ingestion exactly.
- Not tested: the large-scale performance under memory pressure, concurrent
  ingesters.

## Results
- Seed 42, small: 2,057 lead rows (108 rejected, 57 merged duplicates), 8,745
  event lines (8 malformed, 237 duplicates, 18 unknown dispositions, 462
  final orphans — mostly rejected leads and never-uploaded numbers that still
  got calls). Ingest + report: ~1 second.
- Seed 42, large: 143,990 lead rows, 632,602 event lines; ingest + report in
  **42 seconds** on a laptop-class sandbox (stretch: under 2 minutes).
- Notable in the data: roughly a quarter of leads are never called; a visible
  share of "completed" leads end on `not_interested`, which is terminal but
  not success — the two flags genuinely differ.

## If I had another day
1. Make each drop a single transaction (true crash atomicity for counters).
2. Incremental report materialisation so `report` doesn't rescan the ledger.
3. Postgres path with `ON CONFLICT` upserts and a concurrent-ingest test.
4. Property-based tests (hypothesis) for phone/date/decimal fuzzing.
5. A small `verify` command that recomputes report hashes for tamper checks.
