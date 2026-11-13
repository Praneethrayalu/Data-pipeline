"""Unit tests for effective disposition and bucket logic."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.report import Call, _bucket, _effective_disposition  # noqa: E402


def ev(event_id, event_type, occurred_at, code=None):
    import json

    return {
        "id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "payload": json.dumps({"disposition_code": code} if code else {}),
    }


class TestEffectiveDisposition:
    def test_no_disposition(self):
        events = [ev(1, "call.initiated", "2026-10-01T10:00:00+00:00")]
        assert _effective_disposition(events) is None

    def test_latest_set_wins(self):
        events = [
            ev(1, "disposition.set", "2026-10-01T10:00:00+00:00", "callback_requested"),
            ev(2, "disposition.set", "2026-10-01T11:00:00+00:00", "payment_link_sent"),
        ]
        assert _effective_disposition(events) == "payment_link_sent"

    def test_override_beats_set_even_when_set_is_later(self):
        events = [
            ev(1, "disposition.set", "2026-10-03T10:00:00+00:00", "callback_requested"),
            ev(2, "disposition.override", "2026-10-01T10:00:00+00:00", "renewed"),
        ]
        # any override wins over every set, regardless of timestamps
        assert _effective_disposition(events) == "renewed"

    def test_latest_override_wins(self):
        events = [
            ev(1, "disposition.override", "2026-10-01T10:00:00+00:00", "not_interested"),
            ev(2, "disposition.override", "2026-10-02T10:00:00+00:00", "renewed"),
        ]
        assert _effective_disposition(events) == "renewed"


def make_call(connected=False, telephony_failed=False, disposition=None):
    return Call(
        call_id="c1",
        lead_id=1,
        workflow_id="motor_renewal",
        initiated_at=__import__("datetime").datetime(2026, 10, 1),
        connected=connected,
        telephony_failed=telephony_failed,
        effective_disposition=disposition,
    )


class TestBucket:
    CATALOGUE = {
        "catalogue": {
            "motor_renewal": {
                "renewed": {"is_terminal": True, "is_success": True},
                "dnd": {"is_terminal": True, "is_success": False},
                "callback_requested": {"is_terminal": False, "is_success": False},
            }
        }
    }

    def setup_method(self):
        import ledger.report as report_module

        report_module._DISPOSITIONS_CACHE.clear()
        report_module._DISPOSITIONS_CACHE.update(self.CATALOGUE)

    def test_no_calls(self):
        assert _bucket([]) == "yet_to_reach_out"

    def test_completed_on_terminal_disposition(self):
        calls = [make_call(telephony_failed=True), make_call(connected=True, disposition="renewed")]
        assert _bucket(calls) == "completed"

    def test_all_telephony_failed(self):
        calls = [make_call(telephony_failed=True), make_call(telephony_failed=True)]
        assert _bucket(calls) == "in_progress_telephony_failed"

    def test_mixed_is_dialed(self):
        calls = [make_call(telephony_failed=True), make_call(connected=True, disposition="callback_requested")]
        assert _bucket(calls) == "in_progress_dialed"

    def test_call_without_disposition_is_still_dialed(self):
        calls = [make_call(connected=True)]
        assert _bucket(calls) == "in_progress_dialed"

    def test_dnd_is_terminal(self):
        calls = [make_call(connected=True, disposition="dnd")]
        assert _bucket(calls) == "completed"
