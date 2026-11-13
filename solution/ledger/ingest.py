"""Ingestion: lead CSVs, event JSONL streams, orphan relinking.

Ordering guarantees required by the spec:
- drops are processed in the order they are ingested (or name order when a
  parent directory is passed), files in filename order, rows in file order;
- an already-ingested drop is skipped, so re-running ingest changes nothing;
- after every new drop, quarantined orphan events are re-evaluated, because a
  lead may arrive after its events.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db
from .normalise import (
    normalise_header,
    normalise_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
)
from .schemas import WorkflowSchema


def ingest_path(
    conn: sqlite3.Connection,
    schemas: dict[str, WorkflowSchema],
    dispositions: dict,
    path: Path,
) -> list[str]:
    """Ingest a drop directory, or every drop_* child of a parent directory.

    Returns the list of drop names actually ingested (already-ingested drops
    are skipped, which is what makes re-runs a no-op).
    """
    path = Path(path)
    if _is_drop(path):
        drops = [path]
    else:
        drops = sorted(p for p in path.glob("drop_*") if p.is_dir())
        if not drops:
            raise FileNotFoundError(f"no drop_* directories found under {path}")
    done = []
    for drop in drops:
        if _ingest_drop(conn, schemas, dispositions, drop):
            done.append(drop.name)
    return done


def _is_drop(path: Path) -> bool:
    return path.is_dir() and ((path / "leads").is_dir() or (path / "events").is_dir())


def _ingest_drop(
    conn: sqlite3.Connection,
    schemas: dict[str, WorkflowSchema],
    dispositions: dict,
    drop: Path,
) -> bool:
    drop_key = str(drop.resolve())
    if conn.execute(
        "SELECT 1 FROM ingested_drops WHERE drop_path = ?", (drop_key,)
    ).fetchone():
        return False  # already ingested: re-running changes nothing

    # One transaction per drop: a crash or error mid-drop rolls everything
    # back, so the next run re-ingests the drop from a clean state instead of
    # converging on partial counters.
    try:
        leads_dir = drop / "leads"
        if leads_dir.is_dir():
            for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
                schema = schemas.get(wf_dir.name)
                if schema is None:
                    continue  # no schema for this workflow; nothing to validate against
                for csv_file in sorted(wf_dir.glob("*.csv")):
                    _ingest_lead_file(conn, schema, csv_file)

        events_dir = drop / "events"
        if events_dir.is_dir():
            for event_file in sorted(events_dir.glob("*.jsonl")):
                _ingest_event_file(conn, event_file, dispositions)

        _relink_orphans(conn, dispositions)

        conn.execute("INSERT INTO ingested_drops (drop_path) VALUES (?)", (drop_key,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


# --------------------------------------------------------------------------
# Leads
# --------------------------------------------------------------------------


def _parse_field(schema_field, raw_value):
    """Normalise one raw cell. Returns (value, is_valid)."""
    if schema_field.type == "string":
        return raw_value, True
    if schema_field.type == "phone":
        value = normalise_phone(raw_value)
        return value, value is not None
    if schema_field.type == "date":
        value = parse_date(raw_value)
        return value, value is not None
    if schema_field.type == "decimal":
        value = parse_decimal(raw_value)
        return value, value is not None
    if schema_field.type == "enum":
        value = parse_enum(raw_value, list(schema_field.values))
        return value, value is not None
    raise ValueError(f"unknown field type {schema_field.type!r}")


def _ingest_lead_file(conn: sqlite3.Connection, schema: WorkflowSchema, csv_file: Path) -> None:
    batch_id = csv_file.stem
    # utf-8-sig: tolerate a BOM without breaking first-header matching
    with open(csv_file, newline="", encoding="utf-8-sig") as fh:
        rows = csv.reader(fh)
        try:
            header = next(rows)
        except StopIteration:
            return
        # column index -> schema field; columns matching nothing are ignored
        column_fields = [schema.header_map.get(normalise_header(h)) for h in header]

        row_number = 0
        for cells in rows:
            if all(cell.strip() == "" for cell in cells):
                continue  # blank rows are skipped and not counted
            row_number += 1
            db.incr(conn, "lead_rows_total")

            raw_by_field: dict = {}
            for idx, schema_field in enumerate(column_fields):
                if schema_field is None or idx >= len(cells):
                    continue
                value = cells[idx].strip()
                raw_by_field[schema_field.name] = value if value != "" else None

            # Walk fields in schema order; the first failure is the row's reason.
            parsed: dict = {}
            rejection: str | None = None
            pending_warnings = 0
            for schema_field in schema.fields:
                raw_value = raw_by_field.get(schema_field.name)
                if raw_value is None:
                    if schema_field.required:
                        rejection = f"missing_required:{schema_field.name}"
                        break
                    parsed[schema_field.name] = None
                    continue
                value, is_valid = _parse_field(schema_field, raw_value)
                if not is_valid:
                    if schema_field.type == "phone":
                        rejection = "invalid_phone"
                        break
                    if schema_field.required:
                        # A required field whose value cannot be parsed counts as
                        # missing (the spec only names missing_required and
                        # invalid_phone as reasons); noted in SUBMISSION.md.
                        rejection = f"missing_required:{schema_field.name}"
                        break
                    parsed[schema_field.name] = None
                    pending_warnings += 1
                    continue
                parsed[schema_field.name] = value

            if rejection is not None:  # first failure in schema order is the reason
                db.incr(conn, "lead_rows_rejected")
                db.incr_rejection_reason(conn, rejection)
                conn.execute(
                    "INSERT OR IGNORE INTO rejections (workflow_id, batch_id, row_number, reason)"
                    " VALUES (?, ?, ?, ?)",
                    (schema.workflow_id, batch_id, row_number, rejection),
                )
                continue

            if pending_warnings:
                db.incr(conn, "field_warnings", pending_warnings)
            _upsert_lead(conn, schema, batch_id, parsed)


def _upsert_lead(conn: sqlite3.Connection, schema: WorkflowSchema, batch_id: str, parsed: dict) -> None:
    """Create the lead, or merge a duplicate (last write wins on non-null)."""
    phone = parsed["phone"]  # guaranteed present and valid for a non-rejected row
    phone_hmac = db.pseudonymise(phone)
    existing = conn.execute(
        "SELECT id, attrs FROM leads WHERE workflow_id = ? AND phone_hmac = ?",
        (schema.workflow_id, phone_hmac),
    ).fetchone()

    pii = {f.name for f in schema.fields if f.pii}
    new_attrs = {
        name: value
        for name, value in parsed.items()
        if name not in pii and name != schema.reference_field.name
    }
    # Defaults apply after merging, to fields that are still null.
    for f in schema.fields:
        if f.default is not None and parsed.get(f.name) is None and new_attrs.get(f.name) is None:
            if f.name not in pii and f.name != schema.reference_field.name:
                new_attrs.setdefault(f.name, f.default)

    if existing is None:
        conn.execute(
            "INSERT INTO leads (workflow_id, phone_hmac, phone_last4, name_hmac, batch_id,"
            " external_ref, attrs) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                schema.workflow_id,
                phone_hmac,
                phone[-4:],
                db.pseudonymise(parsed["customer_name"]) if parsed.get("customer_name") else None,
                batch_id,
                parsed.get(schema.reference_field.name),
                json.dumps(new_attrs, sort_keys=True),
            ),
        )
        return

    db.incr(conn, "duplicate_lead_rows_merged")
    merged = json.loads(existing["attrs"])
    for name, value in new_attrs.items():
        if value is not None:
            merged[name] = value
    for f in schema.fields:
        if f.default is not None and merged.get(f.name) is None and not f.pii and not f.reference:
            merged[f.name] = f.default
    name_hmac = (
        db.pseudonymise(parsed["customer_name"])
        if parsed.get("customer_name")
        else None
    )
    conn.execute(
        "UPDATE leads SET attrs = ?,"
        " external_ref = COALESCE(?, external_ref),"
        " name_hmac = COALESCE(?, name_hmac) WHERE id = ?",
        (
            json.dumps(merged, sort_keys=True),
            parsed.get(schema.reference_field.name),
            name_hmac,
            existing["id"],
        ),
    )


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def _ingest_event_file(conn: sqlite3.Connection, event_file: Path, dispositions: dict) -> None:
    with open(event_file, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue  # empty lines are not counted at all
            db.incr(conn, "events_total")
            _process_event_line(conn, line, dispositions)


def _process_event_line(conn: sqlite3.Connection, line: str, dispositions: dict) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        db.incr(conn, "malformed_events")
        return
    if not isinstance(event, dict) or not event.get("event_id"):
        db.incr(conn, "malformed_events")
        return

    event_id = event["event_id"]
    if conn.execute("SELECT 1 FROM seen_events WHERE event_id = ?", (event_id,)).fetchone():
        db.incr(conn, "duplicate_events_dropped")
        return
    conn.execute("INSERT INTO seen_events (event_id) VALUES (?)", (event_id,))

    occurred_at = parse_timestamp(str(event.get("occurred_at", "")))
    if occurred_at is None:
        # Spec defines malformed as bad JSON / missing event_id; an unparseable
        # timestamp is treated the same way (noted in SUBMISSION.md).
        db.incr(conn, "malformed_events")
        return

    phone = None
    if event.get("phone"):
        phone = normalise_phone(str(event["phone"]))

    lead_id = _link_event(conn, event.get("event_type", ""), event.get("workflow_id", ""),
                          event.get("call_id"), phone)
    if lead_id is None:
        conn.execute(
            "INSERT OR IGNORE INTO orphans (event_id, event_type, workflow_id, call_id,"
            " occurred_at, phone, payload, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                event.get("event_type"),
                event.get("workflow_id"),
                event.get("call_id"),
                occurred_at.isoformat(),
                phone,
                json.dumps(event.get("payload") or {}),
                line,
            ),
        )
        return
    _accept_event(conn, event, lead_id, occurred_at, dispositions)


def _link_event(
    conn: sqlite3.Connection,
    event_type: str,
    workflow_id: str,
    call_id: str | None,
    phone: str | None,
) -> int | None:
    """Link an event to a lead id, or return None when it is an orphan."""
    if event_type == "disposition.override":
        # Overrides carry no phone; they link through the call's other events.
        if call_id:
            row = conn.execute(
                "SELECT lead_id FROM ledger WHERE call_id = ? ORDER BY id LIMIT 1", (call_id,)
            ).fetchone()
            return row["lead_id"] if row else None
        return None
    if phone is None:
        return None
    row = conn.execute(
        "SELECT id FROM leads WHERE workflow_id = ? AND phone_hmac = ?",
        (workflow_id, db.pseudonymise(phone)),
    ).fetchone()
    return row["id"] if row else None


def _accept_event(
    conn: sqlite3.Connection, event: dict, lead_id: int, occurred_at, dispositions: dict
) -> bool:
    """Store a linked event in the ledger. False when ignored (unknown disposition)."""
    event_type = event.get("event_type", "")
    if event_type.startswith("disposition."):
        code = (event.get("payload") or {}).get("disposition_code", "")
        workflow_id = event.get("workflow_id", "")
        # The catalogue is checked when the event links; unknown codes are ignored.
        if code not in dispositions.get(workflow_id, {}):
            db.incr(conn, "unknown_disposition_events")
            return False
    conn.execute(
        "INSERT OR IGNORE INTO ledger (event_id, event_type, workflow_id, call_id,"
        " occurred_at, lead_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event["event_id"],
            event_type,
            event.get("workflow_id", ""),
            event.get("call_id"),
            occurred_at.isoformat(),
            lead_id,
            json.dumps(event.get("payload") or {}),
        ),
    )
    return True


def _relink_orphans(conn: sqlite3.Connection, dispositions: dict) -> None:
    """Re-evaluate quarantined events; a lead may have arrived in this drop.

    Runs to a fixpoint because an override can only link after another event
    of its call has linked.
    """
    while True:
        moved = 0
        orphans = conn.execute("SELECT * FROM orphans ORDER BY id").fetchall()
        for orphan in orphans:
            lead_id = _link_event(
                conn,
                orphan["event_type"] or "",
                orphan["workflow_id"] or "",
                orphan["call_id"],
                orphan["phone"],
            )
            if lead_id is None:
                continue
            event = json.loads(orphan["raw"])
            occurred_at = datetime.fromisoformat(orphan["occurred_at"])
            conn.execute("DELETE FROM orphans WHERE id = ?", (orphan["id"],))
            _accept_event(conn, event, lead_id, occurred_at, dispositions)
            moved += 1
        if moved == 0:
            return
