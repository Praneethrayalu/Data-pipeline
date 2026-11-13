"""SQLite storage: schema, counters, and PII protection.

PII is never stored in plaintext. Phone numbers are pseudonymised with a
keyed hash (HMAC-SHA256) so events can still be matched to leads, and only
the last four digits are kept for masking. Customer names get the same
treatment because no report needs to recover them; see SUBMISSION.md for the
trade-off.

The key comes from the AU_PII_KEY environment variable. A documented
development default exists so a clean clone runs with zero setup; production
would load the key from a secrets manager.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingested_drops (
    drop_path   TEXT PRIMARY KEY,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    phone_hmac   TEXT NOT NULL,
    phone_last4  TEXT NOT NULL,
    name_hmac    TEXT,
    batch_id     TEXT NOT NULL,
    external_ref TEXT,
    attrs        TEXT NOT NULL DEFAULT '{}',
    UNIQUE (workflow_id, phone_hmac)
);
CREATE TABLE IF NOT EXISTS rejections (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (workflow_id, batch_id, row_number)
);
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY,
    event_id    TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id     TEXT,
    occurred_at TEXT NOT NULL,           -- UTC ISO-8601
    lead_id     INTEGER NOT NULL REFERENCES leads (id),
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ledger_call ON ledger (call_id);
CREATE INDEX IF NOT EXISTS idx_ledger_lead ON ledger (lead_id);
CREATE TABLE IF NOT EXISTS orphans (
    id           INTEGER PRIMARY KEY,
    event_id     TEXT NOT NULL UNIQUE,
    event_type   TEXT,
    workflow_id  TEXT,
    call_id      TEXT,
    occurred_at  TEXT,                   -- UTC ISO-8601
    phone        TEXT,                   -- canonical phone when it normalised
    payload      TEXT,
    raw          TEXT NOT NULL
);
"""

_COUNTER_KEYS = (
    "lead_rows_total",
    "lead_rows_rejected",
    "duplicate_lead_rows_merged",
    "field_warnings",
    "events_total",
    "malformed_events",
    "duplicate_events_dropped",
    "unknown_disposition_events",
)

_DEV_DEFAULT_KEY = "agentic-universe-dev-pii-key"  # documented dev default; override with AU_PII_KEY


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    for key in _COUNTER_KEYS:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, '0')", (key,))
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('rejections_by_reason', '{}')")
    conn.commit()
    return conn


def _pii_key() -> bytes:
    return os.environ.get("AU_PII_KEY", _DEV_DEFAULT_KEY).encode("utf-8")


def pseudonymise(value: str) -> str:
    """Deterministic keyed hash of a PII value. Not reversible without the key."""
    return hmac.new(_pii_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def incr(conn: sqlite3.Connection, key: str, amount: int = 1) -> None:
    conn.execute(
        "UPDATE meta SET value = CAST(value AS INTEGER) + ? WHERE key = ?", (amount, key)
    )


def incr_rejection_reason(conn: sqlite3.Connection, reason: str) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'rejections_by_reason'").fetchone()
    counts = json.loads(row["value"])
    counts[reason] = counts.get(reason, 0) + 1
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'rejections_by_reason'",
        (json.dumps(counts, sort_keys=True),),
    )


def counter(conn: sqlite3.Connection, key: str) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()["value"])
