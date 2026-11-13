"""Normalisation rules from assignment-a-data/README.md ("Rules (the spec)").

Every function returns a normalised value or None when the input is invalid.
None of these functions decide what a rejection or a warning is; that policy
lives in ingest.py. Keeping the pure rules here makes them trivially testable.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

_HEADER_RUN = re.compile(r"[_\s]+")
_NON_DIGIT = re.compile(r"\D+")
_AMOUNT_PREFIX = re.compile(r"^\s*(?:₹|rs\.?|inr)\s*", re.IGNORECASE)

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y")


def normalise_header(raw: str) -> str:
    """Lower-case, trim, and replace every run of '_' or whitespace with one space."""
    return _HEADER_RUN.sub(" ", raw.strip().lower())


def normalise_phone(raw: str) -> str | None:
    """Indian mobile number -> '+91XXXXXXXXXX', or None when invalid.

    Strip every non-digit. 12 digits starting with 91 lose the 91; 11 digits
    starting with 0 lose the 0. The result is valid only when it is exactly
    10 digits starting with 6, 7, 8 or 9.
    """
    digits = _NON_DIGIT.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def parse_date(raw: str) -> str | None:
    """Day-first date in any accepted format -> 'YYYY-MM-DD', or None.

    Accepted: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (month name
    case-insensitive). Impossible dates (31/02/2026) are invalid.
    """
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_decimal(raw: str) -> str | None:
    """Money amount -> canonical string with 2 decimals, or None.

    Optional ₹ / Rs / Rs. / INR prefix (case-insensitive, optional space);
    commas are thousands separators; must be >= 0; rounded half-up to 2 places.
    """
    body = _AMOUNT_PREFIX.sub("", raw.strip()).replace(",", "").strip()
    if not body:
        return None
    try:
        value = Decimal(body)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_enum(raw: str, values: list[str]) -> str | None:
    """Case-insensitive match against allowed values, stored lower-case."""
    candidate = raw.strip().lower()
    return candidate if candidate in {v.lower() for v in values} else None


def parse_timestamp(raw: str) -> datetime | None:
    """Event timestamp -> aware UTC datetime, or None.

    Three forms: UTC ('...Z'), offset ('...+05:30') and naive
    ('2026-10-03 10:01:00'). Naive means IST. Returns None when unparseable.
    """
    s = raw.strip()
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        elif "T" in s:
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    """Aware datetime -> the same instant in IST."""
    return dt.astimezone(IST)
