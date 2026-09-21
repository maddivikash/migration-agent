"""Deterministic normalisers. Each returns (value, confidence, note).

confidence semantics (shared across the agent):
  1.0  -> unambiguous, applied silently (logged)
  0.7  -> applied, but flagged in the audit trail as a judgement call
  <0.5 -> NOT applied; the caller escalates
"""
from __future__ import annotations
import re
from datetime import date, datetime

Result = tuple[str | None, float, str]

_DATE_FORMATS_UNAMBIGUOUS = ["%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%d-%b-%Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y", "%Y%m%d"]


def clean_whitespace(v: str) -> str:
    return re.sub(r"\s+", " ", v).strip()


def clean_email(v: str) -> Result:
    v = clean_whitespace(v).lower()
    if not v:
        return None, 1.0, "blank"
    if re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", v):
        return v, 1.0, "lower-cased"
    return None, 0.0, f"'{v}' is not a valid email"


def clean_phone(v: str, default_cc: str = "91") -> Result:
    raw = clean_whitespace(v)
    if not raw:
        return None, 1.0, "blank"
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]                       # national trunk prefix
    if len(digits) == 10:
        return f"+{default_cc}{digits}", 1.0, "normalised to E.164 (assumed +91)"
    if len(digits) == 12 and digits.startswith(default_cc):
        return f"+{digits}", 1.0, "normalised to E.164"
    if 11 <= len(digits) <= 15:
        return f"+{digits}", 0.7, "normalised to E.164 (country code taken as given)"
    return None, 0.0, f"'{raw}' does not look like a phone number"


def infer_slash_date_order(values: list[str]) -> tuple[str | None, str]:
    """Decide dd/mm vs mm/dd for a whole column from the evidence in it.

    Returns ("dmy"|"mdy"|None, reason). File-level inference avoids escalating
    every single ambiguous row like 03/04/2021 - one decision covers the column.
    """
    a_gt12 = b_gt12 = 0
    for v in values:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", v.strip())
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12: a_gt12 += 1
        if b > 12: b_gt12 += 1
    if a_gt12 and not b_gt12:
        return "dmy", f"{a_gt12} value(s) have a first component > 12, none have a second > 12"
    if b_gt12 and not a_gt12:
        return "mdy", f"{b_gt12} value(s) have a second component > 12, none have a first > 12"
    if a_gt12 and b_gt12:
        return None, "column mixes both orders"
    return None, "no value in the column disambiguates the order"


def clean_date(v: str, slash_order: str | None) -> Result:
    raw = clean_whitespace(v)
    if not raw:
        return None, 1.0, "blank"
    # Excel may hand us a datetime string
    for fmt in _DATE_FORMATS_UNAMBIGUOUS + ["%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(raw, fmt).date().isoformat(), 1.0, f"parsed as {fmt}"
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", raw)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000 if y < 50 else 1900
        if slash_order == "dmy" or (slash_order is None and a > 12 and b <= 12):
            d, mo = a, b
        elif slash_order == "mdy" or (slash_order is None and b > 12 and a <= 12):
            d, mo = b, a
        else:
            return None, 0.3, f"'{raw}' could be dd/mm or mm/dd and the column gives no hint"
        try:
            return date(y, mo, d).isoformat(), 1.0, f"parsed as {'dd/mm/yyyy' if d == a else 'mm/dd/yyyy'}"
        except ValueError:
            return None, 0.0, f"'{raw}' is not a real calendar date"
    return None, 0.0, f"'{raw}' is not a recognisable date"


def clean_number(v: str) -> Result:
    raw = clean_whitespace(v)
    if not raw:
        return None, 1.0, "blank"
    s = re.sub(r"[,\s₹$]|INR|Rs\.?", "", raw, flags=re.I)
    try:
        f = float(s)
        return (str(int(f)) if f.is_integer() else str(f)), 1.0, "parsed number (separators stripped)"
    except ValueError:
        return None, 0.0, f"'{raw}' is not numeric"


def clean_text(v: str, title_case: bool = False) -> Result:
    s = clean_whitespace(v)
    if not s:
        return None, 1.0, "blank"
    if title_case and (s.isupper() or s.islower()):
        return s.title(), 1.0, "fixed casing"
    return s, 1.0, "trimmed"


# --- enum canonicalisation -------------------------------------------------
_ENUM_SYNONYMS: dict[str, dict[str, list[str]]] = {
    "employment_type": {
        "full_time": ["full time", "full-time", "ft", "permanent", "fulltime", "regular"],
        "part_time": ["part time", "part-time", "pt", "parttime"],
        "contractor": ["contractor", "contract", "consultant", "c2c"],
        "intern": ["intern", "internship", "trainee"],
    },
    "status": {
        "active": ["active", "y", "yes", "true", "1", "current", "employed"],
        "terminated": ["terminated", "n", "no", "false", "0", "inactive", "exited", "resigned", "left"],
        "on_leave": ["on leave", "loa", "leave", "leave of absence", "sabbatical"],
    },
    "department": {
        "Engineering": ["eng", "engg", "engineering", "tech", "technology", "r&d", "product engineering"],
        "Sales": ["sls", "sales", "revenue"],
        "Marketing": ["mkt", "mktg", "marketing", "growth"],
        "Finance": ["fin", "finance", "accounts", "accounting"],
        "HR": ["hr", "human resources", "people", "people ops", "talent"],
        "Operations": ["ops", "operations"],
        "Support": ["sup", "support", "customer support", "cs", "customer success"],
    },
}


def clean_enum(field: str, v: str, allowed: list[str]) -> Result:
    s = clean_whitespace(v)
    if not s:
        return None, 1.0, "blank"
    key = s.lower().replace("_", " ")
    for canon in allowed:
        if key == canon.lower().replace("_", " "):
            return canon, 1.0, "exact match"
    for canon, syns in _ENUM_SYNONYMS.get(field, {}).items():
        if canon in allowed and key in syns:
            return canon, 1.0, f"synonym of '{canon}'"
    # prefix match e.g. "Engineering Dept" -> Engineering
    for canon in allowed:
        if key.startswith(canon.lower()) or canon.lower().startswith(key) and len(key) >= 4:
            return canon, 0.7, f"prefix match to '{canon}'"
    return None, 0.2, f"'{s}' is not a known {field} value"


def split_full_name(v: str) -> tuple[str | None, str | None, float, str]:
    s = clean_whitespace(v)
    if not s:
        return None, None, 1.0, "blank"
    if "," in s:                                   # "Sharma, Aarav"
        last, first = [p.strip() for p in s.split(",", 1)]
        return first.title() if first.isupper() or first.islower() else first, last, 1.0, "split on comma as 'Last, First'"
    parts = s.split(" ")
    if len(parts) == 1:
        return None, None, 0.3, f"'{s}' is a single token; cannot tell first from last name"
    if len(parts) == 2:
        return parts[0], parts[1], 1.0, "split on space"
    return parts[0], " ".join(parts[1:]), 0.7, "first token = first name, remainder = last name"
