"""Unit tests for the normalisation rules - the rules most likely to break."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.normalise import (  # noqa: E402
    IST,
    normalise_header,
    normalise_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
)


class TestPhone:
    def test_plain_10_digits(self):
        assert normalise_phone("9876543210") == "+919876543210"

    def test_all_generator_formats(self):
        # every format the data generator emits must land on the same canonical form
        assert normalise_phone("+91 77187 50036") == "+917718750036"
        assert normalise_phone("07718750036") == "+917718750036"
        assert normalise_phone("91-77187-50036") == "+917718750036"
        assert normalise_phone("(+91) 7718750036") == "+917718750036"
        assert normalise_phone("+917718750036") == "+917718750036"

    def test_invalid_phones(self):
        assert normalise_phone("123456789") is None          # 9 digits
        assert normalise_phone("5123456789") is None         # bad prefix
        assert normalise_phone("022-2000-1000") is None      # landline
        assert normalise_phone("N/A") is None
        assert normalise_phone("+1 415 555 0134") is None    # non-Indian

    def test_must_start_6_to_9(self):
        for first in "6789":
            assert normalise_phone(first + "234567890") == f"+91{first}234567890"
        assert normalise_phone("5234567890") is None


class TestDate:
    def test_all_formats(self):
        assert parse_date("2026-12-01") == "2026-12-01"
        assert parse_date("01/12/2026") == "2026-12-01"
        assert parse_date("01-12-2026") == "2026-12-01"
        assert parse_date("01-Dec-2026") == "2026-12-01"
        assert parse_date("01-DEC-2026") == "2026-12-01"
        assert parse_date("01-dec-2026") == "2026-12-01"

    def test_day_first_not_month_first(self):
        # 05/06/2026 is 5 June, not 6 May
        assert parse_date("05/06/2026") == "2026-06-05"

    def test_impossible_dates(self):
        assert parse_date("31/02/2026") is None
        assert parse_date("2026-13-01") is None
        assert parse_date("soon") is None
        assert parse_date("TBD") is None


class TestDecimal:
    def test_prefixes_and_commas(self):
        assert parse_decimal("₹12,345") == "12345.00"
        assert parse_decimal("Rs. 500") == "500.00"
        assert parse_decimal("rs 500") == "500.00"
        assert parse_decimal("INR 99.9") == "99.90"
        assert parse_decimal("inr100") == "100.00"
        assert parse_decimal("1234.5") == "1234.50"
        assert parse_decimal("0") == "0.00"

    def test_invalid(self):
        assert parse_decimal("N/A") is None
        assert parse_decimal("-") is None
        assert parse_decimal("twelve thousand") is None
        assert parse_decimal("-100") is None  # must be >= 0

    def test_rounds_to_two_places(self):
        assert parse_decimal("10.005") == "10.01"  # half-up
        assert parse_decimal("10.004") == "10.00"


class TestEnum:
    VALUES = ["en", "hi", "ta", "kn", "te", "mr"]

    def test_case_insensitive(self):
        assert parse_enum("EN", self.VALUES) == "en"
        assert parse_enum(" ta ", self.VALUES) == "ta"

    def test_invalid_values(self):
        assert parse_enum("Hindi", self.VALUES) is None
        assert parse_enum("english", self.VALUES) is None


class TestHeader:
    def test_normalisation(self):
        assert normalise_header("  Mobile_No ") == "mobile no"
        assert normalise_header("Policy  No") == "policy no"
        assert normalise_header("CUSTOMER_NAME") == "customer name"

    def test_alias_matching(self):
        # "Insured Name" is an alias of customer_name in motor_renewal
        assert normalise_header("Insured Name") == "insured name"


class TestTimestamp:
    def test_utc_z(self):
        dt = parse_timestamp("2026-10-05T13:20:21Z")
        assert dt is not None and dt.utcoffset().total_seconds() == 0

    def test_offset_converts_to_utc(self):
        dt = parse_timestamp("2026-10-05T18:50:21+05:30")
        assert dt is not None
        assert dt.isoformat() == "2026-10-05T13:20:21+00:00"

    def test_naive_means_ist(self):
        dt = parse_timestamp("2026-10-05 18:50:21")
        assert dt is not None
        assert dt.isoformat() == "2026-10-05T13:20:21+00:00"

    def test_ist_date_boundary(self):
        # 01:30 IST on the 6th is still the 5th in UTC; IST date must be the 6th
        dt = parse_timestamp("2026-10-06T01:30:00+05:30")
        assert dt is not None
        assert dt.astimezone(IST).date().isoformat() == "2026-10-06"

    def test_garbage(self):
        assert parse_timestamp("not a timestamp") is None
