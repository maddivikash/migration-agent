"""Propose a source-column -> target-field mapping for one file.

Score = name evidence (alias / fuzzy match) + content evidence (what the values
look like). Decision policy:
  best >= AUTO and (best - runner_up) >= MARGIN  -> apply silently
  best >= AUTO but runner-up is close              -> ambiguous -> LLM tie-break, else escalate
  best <  AUTO but >= WEAK                         -> escalate with suggestion
  best <  WEAK                                     -> drop column (logged, never escalated)
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from rapidfuzz import fuzz
from app.agent.schema import Schema, FieldSpec
from app.agent.ingest import profile_column

AUTO = 0.80      # confident enough to apply without asking
MARGIN = 0.15    # runner-up must be at least this far behind
WEAK = 0.45      # below this the column is judged to have no target


@dataclass
class Candidate:
    field: str
    score: float
    reasons: list[str] = field(default_factory=list)
    name_score: float = 0.0


@dataclass
class ColumnMapping:
    source_column: str
    target: str | None            # None = dropped
    confidence: float
    status: str                   # "auto" | "ambiguous" | "weak" | "dropped" | "human"
    candidates: list[Candidate]
    samples: list[str]
    note: str = ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("_", " ")).strip()


def _name_score(col: str, spec: FieldSpec) -> tuple[float, str]:
    c = _norm(col)
    names = [spec.name.replace("_", " ")] + spec.aliases
    best, why = 0.0, ""
    for n in names:
        n2 = _norm(n)
        if c == n2:
            return 1.0, f"column name equals alias '{n}'"
        # token_sort (not token_set): 'First Name' must NOT fully match alias 'name'
        s = max(fuzz.ratio(c, n2), fuzz.token_sort_ratio(c, n2)) / 100
        if s > best:
            best, why = s, f"column name resembles '{n}' ({int(s*100)}%)"
    return best, why


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
_PHONE = re.compile(r"^[+0-9][0-9 \-()]{7,}$")
_DATE = re.compile(r"^(\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}|[A-Za-z]{3,9} \d{1,2}, \d{4}|\d{1,2}-[A-Za-z]{3}-\d{4})")
_NUM = re.compile(r"^[₹$]?[\d,]+(\.\d+)?$")
_ID = re.compile(r"^[A-Z]{1,3}\d{3,}$")


def _content_score(samples: list[str], spec: FieldSpec) -> tuple[float, str]:
    """How well do the actual values fit the target field's type?"""
    if not samples:
        return 0.0, ""
    def frac(rx): return sum(1 for s in samples if rx.match(s)) / len(samples)
    t = spec.type
    if t == "email":
        f = frac(_EMAIL); return f, f"{int(f*100)}% of values are emails"
    if t == "phone":
        f = frac(_PHONE) * (1 - frac(_EMAIL)); return f, f"{int(f*100)}% of values look like phone numbers"
    if t == "date":
        f = frac(_DATE); return f, f"{int(f*100)}% of values look like dates"
    if t == "number":
        f = frac(_NUM); return f, f"{int(f*100)}% of values are numeric"
    if t == "enum":
        from app.agent.cleaning import clean_enum
        hits = sum(1 for s in samples if clean_enum(spec.name, s, spec.values)[0] is not None)
        f = hits / len(samples); return f, f"{int(f*100)}% of values match allowed {spec.name} values"
    if t == "string":
        if spec.name == "employee_id":
            f = frac(_ID); return f, f"{int(f*100)}% of values look like IDs"
        # generic text: penalise if values are clearly typed as something else
        typed = max(frac(_EMAIL), frac(_DATE), frac(_NUM), frac(_PHONE))
        return 0.5 * (1 - typed), "free text"
    return 0.0, ""


def score_column(col: str, rows: list[dict], schema: Schema) -> list[Candidate]:
    prof = profile_column(rows, col)
    samples = prof["samples"]
    cands = []
    for spec in schema.fields.values():
        ns, nwhy = _name_score(col, spec)
        cs, cwhy = _content_score(samples, spec)
        # names carry most of the signal; content confirms or vetoes
        score = 0.65 * ns + 0.35 * cs
        if ns == 1.0:
            score = 0.85 + 0.15 * cs     # an exact alias hit is strong evidence; content only confirms
        if spec.type in ("email", "date", "phone", "number") and cs < 0.3 and ns < 1.0:
            score *= 0.5      # values contradict the type -> strong veto
        reasons = [r for r in (nwhy, cwhy) if r]
        cands.append(Candidate(spec.name, round(score, 3), reasons, round(ns, 3)))
    cands.sort(key=lambda c: -c.score)
    return cands


def propose_mapping(columns: list[str], rows: list[dict], schema: Schema) -> list[ColumnMapping]:
    out = []
    for col in columns:
        cands = score_column(col, rows, schema)
        samples = profile_column(rows, col)["samples"][:5]
        best, second = cands[0], cands[1]
        if best.score < WEAK:
            out.append(ColumnMapping(col, None, best.score, "dropped", cands[:3], samples,
                                     f"no target field fits (best guess '{best.field}' at {int(best.score*100)}%)"))
        elif best.score >= AUTO and best.score - second.score >= MARGIN:
            out.append(ColumnMapping(col, best.field, best.score, "auto", cands[:3], samples, "; ".join(best.reasons)))
        elif best.score >= AUTO:
            out.append(ColumnMapping(col, best.field, best.score, "ambiguous", cands[:3], samples,
                                     f"'{best.field}' ({int(best.score*100)}%) and '{second.field}' ({int(second.score*100)}%) are both plausible"))
        elif best.name_score < 0.6:
            # only the VALUES fit (e.g. any numeric column looks like 'salary'); the name says
            # nothing. Asking about every such column is noise - drop it, but list it so the
            # consultant can rescue it from the mapping table.
            out.append(ColumnMapping(col, None, best.score, "dropped", cands[:3], samples,
                                     f"values resemble '{best.field}' but the column name does not; not migrated"))
        else:
            out.append(ColumnMapping(col, best.field, best.score, "weak", cands[:3], samples,
                                     f"low confidence: best guess '{best.field}' at {int(best.score*100)}%"))
    # one target field can only be fed by one column per file
    claimed: dict[str, list[ColumnMapping]] = {}
    for m in out:
        if m.target and m.status in ("auto", "ambiguous", "weak"):
            claimed.setdefault(m.target, []).append(m)
    for tgt, ms in claimed.items():
        if len(ms) > 1:
            ms.sort(key=lambda m: (m.status != "auto", -m.confidence))
            winner = ms[0]
            for m in ms[1:]:
                if winner.status == "auto" and m.status != "auto":
                    # a confident column already supplies this field; the weaker one is surplus
                    m.status, m.target = "dropped", None
                    m.note = f"'{tgt}' is already supplied by '{winner.source_column}' with higher confidence; not migrated"
                else:
                    m.status = "ambiguous"
                    m.note = f"'{winner.source_column}' also maps to '{tgt}'; two columns cannot feed one field"
    return out
