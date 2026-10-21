from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc


class Problem(Exception):
    def __init__(self, code, message, *, details=None, exit_code=1):
        super().__init__(message)
        self.code, self.details, self.exit_code = code, details or {}, exit_code


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Problem("invalid_input", "Invalid IANA timezone: " + str(name)) from None


def parse_time(value, tz_name="Asia/Shanghai", *, all_day=False):
    """Reject invalid dates, nonexistent local times and ambiguous DST times."""
    if not isinstance(value, str):
        raise Problem("invalid_input", "Time must be an ISO-8601 string")
    tz = zone(tz_name)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        if not all_day:
            raise Problem("invalid_input", "Date-only values require --all-day")
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            raise Problem("invalid_input", "Invalid date") from None
    if all_day:
        raise Problem("invalid_input", "All-day values must be YYYY-MM-DD")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Problem("invalid_input", "Expected ISO-8601 datetime, e.g. 2026-10-22T20:00:00+08:00") from None
    if parsed.tzinfo is None:
        a, b = parsed.replace(tzinfo=tz, fold=0), parsed.replace(tzinfo=tz, fold=1)
        valid = [v for v in (a, b) if v.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == parsed]
        if not valid:
            raise Problem("invalid_input", "Nonexistent local time during DST change")
        if len(valid) == 2 and a.utcoffset() != b.utcoffset():
            raise Problem("invalid_input", "Ambiguous local time: provide an explicit UTC offset")
        parsed = valid[0]
    return parsed.astimezone(tz).replace(microsecond=0)


def iso(value):
    return value.isoformat() if value is not None else None


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def same_time(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, str) and isinstance(b, str):
        try:
            x = dt.datetime.fromisoformat(a.replace("Z", "+00:00"))
            y = dt.datetime.fromisoformat(b.replace("Z", "+00:00"))
            if x.tzinfo and y.tzinfo:
                return abs((x - y).total_seconds()) < 1
        except ValueError:
            pass
    return a == b


def verify_fields(expected, actual):
    differences = {}
    for k, v in expected.items():
        equal = same_time(v, actual.get(k)) if k in {"start", "end", "due"} else v == actual.get(k)
        if not equal:
            differences[k] = {"expected": v, "actual": actual.get(k)}
    return {"verified": not differences, "scope": "server_readback", "differences": differences,
            "device_notification": "not_observed"}


def readback(read, expected, *, attempts=3, delay=0.4):
    last, error = None, None
    for i in range(attempts):
        try:
            last = read()
            result = verify_fields(expected, last)
            if result["verified"]:
                return last, result
        except Problem as exc:
            if exc.code not in {"not_found", "network_error", "server_error"}:
                raise
            error = exc
        except Exception:
            error = Problem("server_error", "Provider readback was unavailable")
        if i + 1 < attempts:
            time.sleep(delay)
    if last is not None:
        raise Problem("verification_failed", "Server readback differs from the requested values",
                      details={"item": last, "verification": verify_fields(expected, last)}, exit_code=3)
    raise Problem("write_unconfirmed", "Write may have succeeded; do not create again with a new request ID",
                  details={"readback_error": error.code if error else "unavailable"}, exit_code=4)


def choose(items, value):
    matches = [x for x in items if x["id"] == value or x["name"] == value]
    if not matches:
        raise Problem("not_found", "No calendar/list matches the supplied name or full ID")
    if len(matches) != 1:
        raise Problem("ambiguous_target", "Multiple targets share this name; use its full ID")
    return matches[0]


def redact_error(exc):
    # Never serialize provider exceptions: they can contain URLs, cookies or credentials.
    if isinstance(exc, Problem):
        return exc
    return Problem("provider_error", "Provider operation failed (" + type(exc).__name__ + "). Check login and connectivity.")
