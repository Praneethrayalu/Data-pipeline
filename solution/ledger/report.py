"""Report computation: the five output files, derived from the ledger.

Everything here is a pure function of database state, so reports always
reflect everything ingested so far, no matter which order drops arrived in.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path

from . import db
from .normalise import IST, to_ist
from .schemas import load_dispositions


@dataclass
class Call:
    call_id: str
    lead_id: int
    workflow_id: str
    initiated_at: datetime
    connected: bool = False
    telephony_failed: bool = False
    effective_disposition: str | None = None


@dataclass
class LeadView:
    lead_id: int
    workflow_id: str
    batch_id: str
    external_ref: str
    phone_last4: str
    calls: list[Call] = dc_field(default_factory=list)
    bucket: str = "yet_to_reach_out"


def _effective_disposition(events: list[sqlite3.Row]) -> str | None:
    """Latest override by occurred_at wins; otherwise the latest set."""
    overrides = [e for e in events if e["event_type"] == "disposition.override"]
    sets = [e for e in events if e["event_type"] == "disposition.set"]
    pool = overrides if overrides else sets
    if not pool:
        return None
    latest = max(pool, key=lambda e: (e["occurred_at"], e["id"]))
    return json.loads(latest["payload"]).get("disposition_code")


def build_calls(conn: sqlite3.Connection) -> tuple[list[Call], dict[int, LeadView]]:
    """Reconstruct calls (initiated-present groups) and per-lead views."""
    leads = {
        row["id"]: LeadView(
            lead_id=row["id"],
            workflow_id=row["workflow_id"],
            batch_id=row["batch_id"],
            external_ref=row["external_ref"] or "",
            phone_last4=row["phone_last4"],
        )
        for row in conn.execute("SELECT * FROM leads")
    }
    by_call: dict[str, list[sqlite3.Row]] = {}
    for event in conn.execute("SELECT * FROM ledger ORDER BY id"):
        if event["call_id"]:
            by_call.setdefault(event["call_id"], []).append(event)

    calls: list[Call] = []
    for call_id, events in by_call.items():
        initiated = [e for e in events if e["event_type"] == "call.initiated"]
        if not initiated:
            continue  # a call is counted only if its call.initiated is present
        first = min(initiated, key=lambda e: (e["occurred_at"], e["id"]))
        call = Call(
            call_id=call_id,
            lead_id=first["lead_id"],
            workflow_id=first["workflow_id"],
            initiated_at=datetime.fromisoformat(first["occurred_at"]),
            connected=any(e["event_type"] == "call.connected" for e in events),
            telephony_failed=any(e["event_type"] == "call.telephony_failed" for e in events),
            effective_disposition=_effective_disposition(events),
        )
        calls.append(call)
        if call.lead_id in leads:
            leads[call.lead_id].calls.append(call)

    for view in leads.values():
        view.calls.sort(key=lambda c: (c.initiated_at, c.call_id))
        view.bucket = _bucket(view.calls)
    return calls, leads


def _bucket(calls: list[Call]) -> str:
    if not calls:
        return "yet_to_reach_out"
    if any(_disposition_flag(c, "is_terminal") for c in calls):
        return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


_DISPOSITIONS_CACHE: dict[str, dict[str, dict]] = {}


def _disposition_flag(call: Call, flag: str) -> bool:
    code = call.effective_disposition
    if not code:
        return False
    catalogue = _DISPOSITIONS_CACHE.get("catalogue", {})
    info = catalogue.get(call.workflow_id, {}).get(code)
    return bool(info and info[flag])


def write_reports(
    conn: sqlite3.Connection, out_dir: Path, schemas_dir: Path
) -> dict[str, Path]:
    """Write the five report files described by the output contract."""
    raw = load_dispositions(schemas_dir)
    _DISPOSITIONS_CACHE["catalogue"] = {
        wf: {code: {"is_terminal": d.is_terminal, "is_success": d.is_success}
             for code, d in codes.items()}
        for wf, codes in raw.items()
    }
    calls, leads = build_calls(conn)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {
        "batch_summary": _batch_summary(leads, out_dir),
        "daily_overview": _daily_overview(calls, out_dir),
        "lead_report": _lead_report(leads, out_dir),
        "rejections": _rejections(conn, out_dir),
        "data_quality": _data_quality(conn, out_dir),
    }
    return written


def _write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _batch_summary(leads: dict[int, LeadView], out_dir: Path) -> Path:
    groups: dict[tuple[str, str], dict[str, int]] = {}
    for view in leads.values():
        counts = groups.setdefault(
            (view.workflow_id, view.batch_id),
            {"total": 0, "yet_to_reach_out": 0, "in_progress_telephony_failed": 0,
             "in_progress_dialed": 0, "completed": 0},
        )
        counts["total"] += 1
        counts[view.bucket] += 1
    rows = [
        [wf, batch, c["total"], c["yet_to_reach_out"], c["in_progress_telephony_failed"],
         c["in_progress_dialed"], c["completed"]]
        for (wf, batch), c in sorted(groups.items())
    ]
    return _write_csv(
        out_dir / "batch_summary.csv",
        ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
         "in_progress_telephony_failed", "in_progress_dialed", "completed"],
        rows,
    )


def _daily_overview(calls: list[Call], out_dir: Path) -> Path:
    days: dict[tuple[str, str], dict] = {}
    for call in calls:
        day = to_ist(call.initiated_at).date().isoformat()
        bucket = days.setdefault(
            (call.workflow_id, day),
            {"total": 0, "connected": 0, "success_leads": set()},
        )
        bucket["total"] += 1
        if call.connected:
            bucket["connected"] += 1
        if _disposition_flag(call, "is_success"):
            bucket["success_leads"].add(call.lead_id)
    rows = []
    for (wf, day), b in sorted(days.items()):
        rate = b["connected"] / b["total"]
        rows.append([wf, day, b["total"], b["connected"], f"{rate:.4f}",
                     len(b["success_leads"])])
    return _write_csv(
        out_dir / "daily_overview.csv",
        ["workflow_id", "date", "total_calls", "connected_calls",
         "connectivity_rate", "success_leads"],
        rows,
    )


def _lead_report(leads: dict[int, LeadView], out_dir: Path) -> Path:
    rows = []
    for view in leads.values():
        calls = view.calls
        last_call = max(calls, key=lambda c: (c.initiated_at, c.call_id)) if calls else None
        current_disposition = ""
        for call in reversed(calls):
            if call.effective_disposition:
                current_disposition = call.effective_disposition
                break
        rows.append([
            view.workflow_id,
            view.external_ref,
            view.batch_id,
            f"+91******{view.phone_last4}",
            view.bucket,
            len(calls),
            sum(1 for c in calls if c.connected),
            to_ist(last_call.initiated_at).isoformat() if last_call else "",
            current_disposition,
            "true" if any(_disposition_flag(c, "is_success") for c in calls) else "false",
        ])
    rows.sort(key=lambda r: (r[0], r[1]))
    return _write_csv(
        out_dir / "lead_report.csv",
        ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
         "total_calls", "connected_calls", "last_call_at", "current_disposition",
         "ever_success"],
        rows,
    )


def _rejections(conn: sqlite3.Connection, out_dir: Path) -> Path:
    rows = [
        [r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]]
        for r in conn.execute(
            "SELECT * FROM rejections ORDER BY workflow_id, batch_id, row_number"
        )
    ]
    return _write_csv(
        out_dir / "rejections.csv",
        ["workflow_id", "batch_id", "row_number", "reason"],
        rows,
    )


def _data_quality(conn: sqlite3.Connection, out_dir: Path) -> Path:
    reasons = json.loads(
        conn.execute("SELECT value FROM meta WHERE key = 'rejections_by_reason'").fetchone()["value"]
    )
    orphan_count = conn.execute("SELECT COUNT(*) AS n FROM orphans").fetchone()["n"]
    payload = {
        "lead_rows_total": db.counter(conn, "lead_rows_total"),
        "lead_rows_rejected": db.counter(conn, "lead_rows_rejected"),
        "rejections_by_reason": {k: reasons[k] for k in sorted(reasons)},
        "duplicate_lead_rows_merged": db.counter(conn, "duplicate_lead_rows_merged"),
        "field_warnings": db.counter(conn, "field_warnings"),
        "events_total": db.counter(conn, "events_total"),
        "malformed_events": db.counter(conn, "malformed_events"),
        "duplicate_events_dropped": db.counter(conn, "duplicate_events_dropped"),
        "orphan_events": orphan_count,
        "unknown_disposition_events": db.counter(conn, "unknown_disposition_events"),
    }
    path = out_dir / "data_quality.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
