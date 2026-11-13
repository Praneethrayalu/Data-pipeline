# Solution - Assignment A: Interaction Ledger & Reporting Pipeline

Python 3.11+, standard library only (pytest is needed just for the tests).

## Run

```bash
# from the repo root
python solution/pipeline.py ingest data/drop_1 --db ledger.db
python solution/pipeline.py ingest data/drop_2 --db ledger.db
python solution/pipeline.py report --out reports/ --db ledger.db
```

- `ingest` also accepts a parent directory (`data`) and processes its
  `drop_*` children in name order.
- Re-ingesting a drop that was already ingested changes nothing.
- `report` always reflects everything ingested so far and writes
  `batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`,
  `rejections.csv` and `data_quality.json` into `--out`.
- Set `AU_PII_KEY` to override the development PII key (see `ledger/db.py`).

## Test

```bash
pip install -r solution/requirements.txt
python -m pytest solution/tests -q
```

## Layout

```
solution/
├── pipeline.py            # CLI entry point (ingest / report)
└── ledger/
    ├── normalise.py       # phone/date/decimal/enum/header/timestamp rules
    ├── schemas.py         # workflow schema + disposition catalogue loading
    ├── db.py              # SQLite schema, counters, PII pseudonymisation
    ├── ingest.py          # lead/event ingestion, orphan relinking
    └── report.py          # the five report files
└── tests/                 # pytest: rule unit tests + end-to-end fixture
```
