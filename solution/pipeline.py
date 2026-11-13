#!/usr/bin/env python3
"""Interaction Ledger & Reporting Pipeline - Assignment A.

Usage:
    python solution/pipeline.py ingest data/drop_1 [--db ledger.db]
    python solution/pipeline.py ingest data/drop_2 [--db ledger.db]
    python solution/pipeline.py report --out reports/ [--db ledger.db]

`ingest` also accepts a parent directory containing drop_* folders (they are
processed in name order). Re-ingesting a drop that was already ingested is a
no-op. `report` always reflects everything ingested so far.

Set AU_PII_KEY to control the key used to pseudonymise PII (see ledger/db.py).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python solution/pipeline.py ...` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import db, ingest, report  # noqa: E402
from ledger.schemas import load_dispositions, load_schemas  # noqa: E402

DEFAULT_SCHEMAS = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    parser.add_argument("--db", default="ledger.db", help="SQLite database path")
    parser.add_argument(
        "--schemas", type=Path, default=DEFAULT_SCHEMAS,
        help="directory with workflow schemas and dispositions.csv",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_cmd = sub.add_parser("ingest", help="ingest a drop (or a parent of drop_* dirs)")
    ingest_cmd.add_argument("drop", type=Path)

    report_cmd = sub.add_parser("report", help="write the five report files")
    report_cmd.add_argument("--out", type=Path, required=True)

    args = parser.parse_args()

    schemas = load_schemas(args.schemas)
    dispositions = load_dispositions(args.schemas)
    conn = db.connect(args.db)

    if args.command == "ingest":
        done = ingest.ingest_path(conn, schemas, dispositions, args.drop)
        if done:
            print(f"ingested: {', '.join(done)}")
        else:
            print("nothing to do: drop already ingested")
        return 0

    written = report.write_reports(conn, args.out, args.schemas)
    for name, path in written.items():
        print(f"wrote {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
