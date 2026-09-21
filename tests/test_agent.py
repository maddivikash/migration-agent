"""Behavioural tests for the escalation boundary - the part the panel will ask about."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
from app.agent import cleaning as C
from app.agent.schema import load_schema
from app.agent.memory import Memory
from app.agent.llm import LLM
from app.agent.runner import Agent

ROOT = Path(__file__).resolve().parent.parent
FILES = [str(ROOT / "sample_data" / f) for f in ("legacy_hris_employees.csv", "crm_contacts.xlsx", "payroll_dump.csv")]


def run(decisions=None, tmp_path=Path("/tmp")):
    schema = load_schema(ROOT / "target_schema.yaml")
    llm = LLM(); llm.available = False
    return Agent(schema, Memory(tmp_path / "mem.json"), llm, lambda *a: None, decisions or {}).run(FILES)


# ---- deterministic cleaners: what the agent fixes alone
def test_phone_formats_normalise_to_e164():
    for raw in ("9876543210", "+91 98765 43210", "+91-9876543210", "98765-43210", "09876543210"):
        assert C.clean_phone(raw)[0] == "+919876543210"

def test_unambiguous_dates_parse_silently():
    assert C.clean_date("Jan 15, 2024", None) == ("2024-01-15", 1.0, "parsed as %b %d, %Y")
    assert C.clean_date("2024/01/15", None)[0] == "2024-01-15"
    assert C.clean_date("15-Jan-2024", None)[0] == "2024-01-15"

def test_ambiguous_slash_date_without_column_evidence_is_not_guessed():
    val, conf, _ = C.clean_date("03/04/2021", None)
    assert val is None and conf < 0.5

def test_column_evidence_resolves_slash_order():
    assert C.infer_slash_date_order(["03/04/2021", "25/12/2020"])[0] == "dmy"
    assert C.infer_slash_date_order(["03/04/2021", "12/25/2020"])[0] == "mdy"
    assert C.infer_slash_date_order(["03/04/2021", "05/06/2021"])[0] is None

def test_impossible_date_is_rejected_not_coerced():
    val, conf, _ = C.clean_date("31/02/2020", "dmy")
    assert val is None and conf == 0.0

def test_enum_synonyms_are_applied_but_unknowns_are_not():
    assert C.clean_enum("department", "ENG", ["Engineering"])[0] == "Engineering"
    assert C.clean_enum("status", "Y", ["active", "terminated"])[0] == "active"
    assert C.clean_enum("employment_type", "FT", ["full_time"])[0] == "full_time"
    assert C.clean_enum("department", "BD", ["Engineering", "Sales"])[0] is None

def test_name_splitting_confidence():
    assert C.split_full_name("Sharma, Aarav")[:2] == ("Aarav", "Sharma")
    assert C.split_full_name("Aarav Sharma")[:2] == ("Aarav", "Sharma")
    first, last, conf, _ = C.split_full_name("Anjali Devi Kapoor")
    assert (first, last) == ("Anjali", "Devi Kapoor") and conf == 0.7      # applied, flagged
    assert C.split_full_name("Madonna")[2] < 0.5                            # escalated


# ---- end to end on the sample data
def test_three_files_reconcile_into_one_dataset(tmp_path):
    res = run(tmp_path=tmp_path)
    assert res.stats["source_rows"] == 115
    assert 47 <= res.stats["unique_people"] <= 48        # 45 real + Madonna + Fatima (+ fuzzy dup until confirmed)
    # every source column that clearly matches was mapped without a human
    for f, maps in res.mappings.items():
        auto = {m["source_column"] for m in maps if m["status"] == "auto"}
        assert len(auto) >= 9, f

def test_only_genuine_ambiguities_are_escalated(tmp_path):
    res = run(tmp_path=tmp_path)
    open_ = [e for e in res.escalations if e.status == "open"]
    types = {e.type for e in open_}
    # each planted problem produced exactly its kind of escalation ...
    assert "unknown_value" in types          # department code BD
    assert "conflict" in types               # hire_date differs between HRIS and CRM
    assert "possible_duplicate" in types     # typo email, same name+DOB
    assert "validation_failed" in types      # impossible date / terminated w/o date / single-token name
    assert "ambiguous_mapping" in types      # 'Personal Email' fits both email fields; no exact alias
    # ... and the total stays small: 115 rows in, a handful of questions out
    assert len(open_) <= 8
    # BD appears on two payroll rows, but E1010's department is known from the HRIS file,
    # so only the record with NO other source is blocked - and it asks once per value, not per row
    bd = next(e for e in open_ if e.type == "unknown_value")
    assert bd.affected == ["rohan.reddy@acmecorp.example"]
    # clearly-named columns were never asked about
    asked = {e.context.get("column") for e in open_ if e.type == "ambiguous_mapping"}
    assert asked == {"Personal Email"}

def test_decisions_are_applied_and_learned(tmp_path):
    res = run(tmp_path=tmp_path)
    dec = {}
    for e in res.escalations:
        if e.type == "unknown_value": dec[e.id] = {"value": "Sales"}
        if e.type == "possible_duplicate": dec[e.id] = {"value": "merge"}
        if e.type == "conflict": dec[e.id] = {"value": e.options[1]["value"]}
    res2 = run(dec, tmp_path)
    assert res2.stats["open_escalations"] == res.stats["open_escalations"] - 3
    assert res2.stats["unique_people"] == res.stats["unique_people"] - 1   # duplicate merged
    assert next(r for r in res2.records if r.fields.get("employee_id") == "E1003").fields["department"] == "Sales"

def test_exact_duplicates_are_dropped_silently(tmp_path):
    events = []
    schema = load_schema(ROOT / "target_schema.yaml"); llm = LLM(); llm.available = False
    Agent(schema, Memory(tmp_path / "m.json"), llm, lambda k, m, d: events.append((k, m)), {}).run(FILES)
    assert any(k == "dedupe" and "3 exact duplicate" in m for k, m in events)
