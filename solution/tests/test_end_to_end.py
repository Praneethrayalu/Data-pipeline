"""End-to-end tests on a hand-built fixture that exercises the messy rules:
rejection reasons, duplicate merging, alias headers, blank rows, orphans that
link in a later drop, phone-less overrides, unknown dispositions, duplicate
and malformed event lines, idempotent re-ingest, and drop-order independence.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import db, ingest, report  # noqa: E402
from ledger.schemas import load_dispositions, load_schemas  # noqa: E402

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


def write_leads(drop: Path, workflow: str, batch: str, header: list[str], rows: list) -> None:
    path = drop / "leads" / workflow / f"{batch}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def write_events(drop: Path, name: str, lines: list) -> None:
    path = drop / "events" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ev(eid, etype, wf, call, at, phone=None, payload=None):
    event = {"event_id": eid, "event_type": etype, "workflow_id": wf,
             "call_id": call, "occurred_at": at, "payload": payload or {}}
    if phone is not None:
        event["phone"] = phone
    return json.dumps(event)


@pytest.fixture()
def fixture(tmp_path):
    data = tmp_path / "data"

    # drop_1 leads (canonical headers): two good leads, one bad phone,
    # one missing name, one blank row.
    write_leads(
        data / "drop_1", "motor_renewal", "batch_001",
        ["customer_name", "phone", "policy_number", "policy_expiry",
         "premium_amount", "preferred_language"],
        [
            ["Aarav Sharma", "+91 98765 00001", "MOT/2026/000001", "01-Dec-2026",
             "₹12,000", "EN"],
            ["", "", "", "", "", ""],  # blank row: skipped, not counted
            ["Meera Nair", "077187 50002".replace(" ", ""), "MOT/2026/000002",
             "31/02/2026", "N/A", "Hindi"],  # bad date + bad amount + bad enum = 3 warnings
            ["Bad Phone", "022-2000-1000", "MOT/2026/000003", "", "", ""],  # invalid_phone
            ["", "9876500004", "MOT/2026/000004", "", "", ""],  # missing_required:customer_name
        ],
    )
    # drop_2 leads: alias headers, a duplicate of lead 1 (language change),
    # and lead 3 whose events already arrived in drop_1 (orphans).
    write_leads(
        data / "drop_2", "motor_renewal", "batch_002",
        ["Insured Name", "Mobile No", "Policy No", "Expiry Date", "Premium",
         "Language", "agent_notes"],
        [
            ["Aarav S.", "9876500001", "MOT/2026/000001", "02-Dec-2026", "13000",
             "hi", "duplicate row"],
            ["Kavya Rao", "9876500005", "MOT/2026/000005", "03-Dec-2026", "9000", "te"],
        ],
    )

    # drop_1 events: a completed call for lead 1, a telephony failure for
    # lead 2, events for lead 3 (orphan until drop_2), a duplicate line,
    # a malformed line, an unknown disposition.
    lead1_call = [
        ev("evt_1", "call.initiated", "motor_renewal", "call_1",
           "2026-10-02T10:00:00Z", "9876500001"),
        ev("evt_2", "call.connected", "motor_renewal", "call_1",
           "2026-10-02T10:00:20Z", "9876500001"),
        ev("evt_3", "call.ended", "motor_renewal", "call_1", "2026-10-02T10:05:00Z",
           "9876500001", {"duration_sec": 280, "end_reason": "completed"}),
        ev("evt_4", "disposition.set", "motor_renewal", "call_1",
           "2026-10-02T10:06:00Z", "9876500001", {"disposition_code": "callback_requested"}),
    ]
    lead2_call = [
        ev("evt_5", "call.initiated", "motor_renewal", "call_2",
           "2026-10-02T11:00:00Z", "7718750002"),
        ev("evt_6", "call.telephony_failed", "motor_renewal", "call_2",
           "2026-10-02T11:00:05Z", "7718750002", {"reason": "carrier_rejected"}),
    ]
    orphan_for_lead3 = [
        ev("evt_7", "call.initiated", "motor_renewal", "call_3",
           "2026-10-03T09:00:00Z", "9876500005"),
        ev("evt_8", "call.connected", "motor_renewal", "call_3",
           "2026-10-03T09:00:10Z", "9876500005"),
    ]
    unknown_disp = ev("evt_9", "disposition.set", "motor_renewal", "call_1",
                      "2026-10-02T10:07:00Z", "9876500001",
                      {"disposition_code": "RENEWD"})
    write_events(data / "drop_1", "events_0001.jsonl",
                 lead1_call + lead2_call + orphan_for_lead3
                 + [lead1_call[0]]          # exact duplicate line
                 + ['{"event_id": "evt_bad"']  # malformed JSON
                 + [unknown_disp])

    # drop_2 events: a phone-less override for call_1 (arrives after its call).
    write_events(data / "drop_2", "events_1001.jsonl", [
        ev("evt_10", "disposition.override", "motor_renewal", "call_1",
           "2026-10-04T09:00:00Z", None,
           {"disposition_code": "renewed", "overridden_by": "sup_1",
            "reason": "qa_review"}),
    ])
    return data


def run_pipeline(db_path: Path, *paths: Path) -> dict:
    conn = db.connect(db_path)
    schemas = load_schemas(SCHEMAS_DIR)
    dispositions = load_dispositions(SCHEMAS_DIR)
    for path in paths:
        ingest.ingest_path(conn, schemas, dispositions, path)
    out = db_path.parent / f"reports_{db_path.stem}"
    report.write_reports(conn, out, SCHEMAS_DIR)
    return {p.name: p.read_text() for p in sorted(out.iterdir())}


def read_csv(text):
    return list(csv.reader(text.strip().splitlines()))


def test_full_fixture(fixture, tmp_path):
    reports = run_pipeline(tmp_path / "main.db", fixture / "drop_1", fixture / "drop_2")

    quality = json.loads(reports["data_quality.json"])
    # 4 non-blank lead rows in drop_1 (blank skipped) + 2 in drop_2
    assert quality["lead_rows_total"] == 6
    assert quality["lead_rows_rejected"] == 2
    assert quality["rejections_by_reason"] == {
        "invalid_phone": 1, "missing_required:customer_name": 1}
    assert quality["duplicate_lead_rows_merged"] == 1
    assert quality["field_warnings"] == 3  # bad date, bad amount, bad enum on lead 2
    assert quality["events_total"] == 12  # 11 lines in drop_1 + 1 override in drop_2
    assert quality["malformed_events"] == 1
    assert quality["duplicate_events_dropped"] == 1
    assert quality["unknown_disposition_events"] == 1
    assert quality["orphan_events"] == 0  # lead 3 arrived in drop_2: orphans linked

    leads = read_csv(reports["lead_report.csv"])
    assert leads[0] == ["workflow_id", "external_ref", "batch_id", "phone_masked",
                        "bucket", "total_calls", "connected_calls", "last_call_at",
                        "current_disposition", "ever_success"]
    by_ref = {row[1]: row for row in leads[1:]}

    lead1 = by_ref["MOT/2026/000001"]
    assert lead1[2] == "batch_001"            # first-seen batch wins permanently
    assert lead1[3] == "+91******0001"
    assert lead1[4] == "completed"            # override made it terminal
    assert lead1[5] == "1" and lead1[6] == "1"
    assert lead1[7] == "2026-10-02T15:30:00+05:30"  # 10:00 UTC -> IST
    assert lead1[8] == "renewed"              # override beats the earlier set
    assert lead1[9] == "true"

    lead2 = by_ref["MOT/2026/000002"]
    assert lead2[4] == "in_progress_telephony_failed"
    assert lead2[8] == "" and lead2[9] == "false"

    lead3 = by_ref["MOT/2026/000005"]
    assert lead3[2] == "batch_002"
    assert lead3[4] == "in_progress_dialed"   # connected, no disposition yet
    assert lead3[7] == "2026-10-03T14:30:00+05:30"

    daily = read_csv(reports["daily_overview.csv"])
    overview = {(row[0], row[1]): row for row in daily[1:]}
    # lead 1's call_1 ends up with effective disposition renewed (override in drop_2)
    assert overview[("motor_renewal", "2026-10-02")][2:] == ["2", "1", "0.5000", "1"]
    assert overview[("motor_renewal", "2026-10-03")][2:] == ["1", "1", "1.0000", "0"]

    batch = read_csv(reports["batch_summary.csv"])
    summary = {(row[0], row[1]): row[2:] for row in batch[1:]}
    assert summary[("motor_renewal", "batch_001")] == ["2", "0", "1", "0", "1"]
    assert summary[("motor_renewal", "batch_002")] == ["1", "0", "0", "1", "0"]

    rejections = read_csv(reports["rejections.csv"])
    assert rejections[1:] == [
        ["motor_renewal", "batch_001", "3", "invalid_phone"],
        ["motor_renewal", "batch_001", "4", "missing_required:customer_name"],
    ]


def test_orphan_counts_before_lead_arrives(fixture, tmp_path):
    reports = run_pipeline(tmp_path / "partial.db", fixture / "drop_1")
    quality = json.loads(reports["data_quality.json"])
    assert quality["orphan_events"] == 2  # lead 3's events still quarantined


def test_reingest_changes_nothing(fixture, tmp_path):
    once = run_pipeline(tmp_path / "once.db", fixture / "drop_1", fixture / "drop_2")
    twice = run_pipeline(
        tmp_path / "twice.db",
        fixture / "drop_1", fixture / "drop_2", fixture / "drop_1", fixture / "drop_2",
    )
    assert once == twice


def test_drop_order_and_batching_independence(fixture, tmp_path):
    sequential = run_pipeline(tmp_path / "seq.db", fixture / "drop_1", fixture / "drop_2")
    at_once = run_pipeline(tmp_path / "bulk.db", fixture)
    assert sequential == at_once


def test_pii_not_stored_in_plaintext(fixture, tmp_path):
    run_pipeline(tmp_path / "pii.db", fixture / "drop_1")
    raw = (tmp_path / "pii.db").read_bytes()
    assert b"Aarav" not in raw and b"9876500001" not in raw and b"+919876500001" not in raw


def test_crash_mid_drop_rolls_back(fixture, tmp_path, monkeypatch):
    """A failure mid-drop must leave no partial state behind."""
    import ledger.ingest as ingest_module

    conn = db.connect(tmp_path / "crash.db")
    schemas = load_schemas(SCHEMAS_DIR)
    dispositions = load_dispositions(SCHEMAS_DIR)

    original = ingest_module._ingest_event_file

    def bomb(conn, event_file, dispositions):
        original(conn, event_file, dispositions)  # do real work first
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(ingest_module, "_ingest_event_file", bomb)
    with pytest.raises(RuntimeError):
        ingest.ingest_path(conn, schemas, dispositions, fixture / "drop_1")

    # rolled back: no drop recorded, no counters moved, no leads, no orphans
    assert conn.execute("SELECT COUNT(*) AS n FROM ingested_drops").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM orphans").fetchone()["n"] == 0
    assert db.counter(conn, "lead_rows_total") == 0
    assert db.counter(conn, "events_total") == 0

    # and the same drop ingests cleanly afterwards
    monkeypatch.setattr(ingest_module, "_ingest_event_file", original)
    conn2 = db.connect(tmp_path / "crash.db")
    assert ingest.ingest_path(conn2, schemas, dispositions, fixture / "drop_1") == ["drop_1"]
    assert db.counter(conn2, "lead_rows_total") > 0
