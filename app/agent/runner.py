"""The migration agent.

run(files, schema, decisions, memory, llm, emit) -> RunResult

The whole pipeline is a deterministic function of (source files, schema, human
decisions so far). Resolving an escalation = adding a decision and re-running.
That keeps the audit trail honest (every state is reproducible) and makes the
UI trivially consistent.

Escalation policy (the "line"):
  ESCALATE when a wrong guess would corrupt identity or be hard to undo AND the
  evidence does not favour one answer:
    - a column that fits two target fields about equally (or fits none well but
      isn't obviously junk)
    - a date column whose dd/mm vs mm/dd order cannot be inferred from its values
    - a value that fails cleaning twice (first pass + retry with alternate strategy)
    - an enum value with no synonym match and no confident LLM suggestion
    - conflicting values for the same person across sources on an identity-
      critical field (hire_date, date_of_birth, employee_id)
    - a probable duplicate that is not an exact match (same name+DOB, different email)
    - a record that violates a business rule the agent cannot fix by itself
    - a single-token full name (cannot split)
  HANDLE ALONE (and log) when the fix is mechanical and reversible from the audit
  trail: whitespace, casing, date parsing with evidence, phone formatting, enum
  synonyms, exact duplicate rows, conflicts on soft fields (take source priority),
  columns with no target (drop), 3-token names (first + rest).
"""
from __future__ import annotations
import hashlib, time
from dataclasses import dataclass, field, asdict
from typing import Callable
from .schema import Schema
from .ingest import read_source, SourceTable
from .mapping import propose_mapping, ColumnMapping
from . import cleaning as C
from .llm import LLM
from .memory import Memory

CRITICAL_FIELDS = {"employee_id", "email", "hire_date", "date_of_birth"}
LLM_TRUST = 0.85   # LLM suggestion must be at least this confident to act on alone


@dataclass
class Change:
    field: str
    before: str | None
    after: str | None
    note: str
    actor: str = "agent"     # agent | human | memory | llm
    confidence: float = 1.0


@dataclass
class Record:
    key: str
    fields: dict
    sources: list[dict] = field(default_factory=list)   # {file, row}
    changes: list[Change] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)      # human-readable validation problems
    escalations: list[str] = field(default_factory=list)
    status: str = "ready"                                # ready | held | rejected | pushed | failed | rolled_back
    push: dict | None = None


@dataclass
class Escalation:
    id: str
    type: str
    title: str
    summary: str
    context: dict
    options: list[dict]                 # {id, label, value?}
    custom: dict | None = None          # {"label": str, "fields": [names]} when free-text input is allowed
    affected: list[str] = field(default_factory=list)   # record keys
    severity: str = "medium"            # low | medium | high
    status: str = "open"
    resolution: dict | None = None


@dataclass
class RunResult:
    files: list[dict]
    mappings: dict[str, list[dict]]
    records: list[Record]
    escalations: list[Escalation]
    stats: dict


EmitFn = Callable[[str, str, dict], None]   # (kind, message, details)


def _key_for(fields: dict, file: str, row: int) -> str:
    if fields.get("email"):
        return fields["email"]
    if fields.get("employee_id"):
        return f"id:{fields['employee_id']}"
    return f"{file}:row{row}"


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:8]


class Agent:
    def __init__(self, schema: Schema, memory: Memory, llm: LLM, emit: EmitFn, decisions: dict, step_delay: float = 0.0):
        self.schema, self.memory, self.llm, self.emit = schema, memory, llm, emit
        self.decisions = decisions
        self.escalations: dict[str, Escalation] = {}
        self.delay = step_delay

    # ------------------------------------------------------------------ utils
    def _pause(self):
        if self.delay:
            time.sleep(self.delay)

    def escalate(self, e: Escalation) -> Escalation | None:
        """Register an escalation unless a decision already resolves it."""
        d = self.decisions.get(e.id)
        if d:
            e.status, e.resolution = "resolved", d
        else:
            self.emit("escalation", f"Escalated: {e.title}", {"id": e.id, "type": e.type})
        self.escalations[e.id] = e
        return e

    def decided(self, eid: str) -> dict | None:
        return self.decisions.get(eid)

    # -------------------------------------------------------------- 1. ingest
    def ingest(self, paths: list[str]) -> list[SourceTable]:
        tables = []
        for p in paths:
            t = read_source(p)
            tables.append(t)
            self.emit("ingest", f"Read {t.name}: {len(t.rows)} rows, {len(t.columns)} columns", {"file": t.name, "columns": t.columns})
            self._pause()
        return tables

    # ----------------------------------------------------------------- 2. map
    def map_columns(self, t: SourceTable) -> list[ColumnMapping]:
        maps = propose_mapping(t.columns, t.rows, self.schema)
        desc = {n: f.description or f"{f.type}" + (f" one of {f.values}" if f.values else "") for n, f in self.schema.fields.items()}
        for m in maps:
            eid = f"map:{t.name}:{m.source_column}"
            remembered = self.memory.column(m.source_column)
            d = self.decided(eid)
            if d:                                            # human already decided this column
                m.target = None if d.get("value") in (None, "__drop__") else d["value"]
                m.status, m.confidence, m.note = "human", 1.0, "resolved by consultant"
                self.emit("map", f"{t.name}: '{m.source_column}' -> {m.target or 'DROP'} (consultant decision)", {"file": t.name})
                # keep the escalation visible as resolved
                self.escalate(self._mapping_escalation(t, m, eid))
            elif m.status in ("ambiguous", "weak") and remembered is not None:
                m.target, m.status, m.confidence = (remembered or None), "memory", 1.0
                m.note = "reused a mapping a consultant confirmed on an earlier run"
                self.emit("map", f"{t.name}: '{m.source_column}' -> {m.target or 'DROP'} (from mapping memory)", {"file": t.name})
            elif m.status == "auto":
                self.emit("map", f"{t.name}: '{m.source_column}' -> {m.target} ({int(m.confidence*100)}%)", {"file": t.name, "reasons": m.candidates[0].reasons})
            elif m.status == "dropped":
                self.emit("map", f"{t.name}: '{m.source_column}' dropped - {m.note}", {"file": t.name, "samples": m.samples})
            else:                                            # ambiguous / weak -> LLM tie-break, else human
                cand_names = [c.field for c in m.candidates]
                llm = self.llm.tie_break_column(m.source_column, m.samples, cand_names, desc) if self.llm.available else None
                if llm and llm["field"] == m.candidates[0].field and llm["confidence"] >= LLM_TRUST and m.status == "ambiguous":
                    m.status, m.note = "auto", f"heuristic and LLM agree on '{m.target}' ({llm['reason']})"
                    self.emit("map", f"{t.name}: '{m.source_column}' -> {m.target} (LLM confirmed tie-break)", {"file": t.name, "llm": llm})
                else:
                    m.target = None                          # nothing flows from this column until a human decides
                    e = self._mapping_escalation(t, m, eid, llm)
                    self.escalate(e)
            self._pause()
        return maps

    def _mapping_escalation(self, t: SourceTable, m: ColumnMapping, eid: str, llm: dict | None = None) -> Escalation:
        opts = [{"id": c.field, "label": f"{c.field}  ({int(c.score*100)}%)", "value": c.field, "reasons": c.reasons} for c in m.candidates]
        opts.append({"id": "__drop__", "label": "Don't migrate this column", "value": "__drop__"})
        ctx = {"file": t.name, "column": m.source_column, "sample_values": m.samples, "why": m.note}
        if llm:
            ctx["llm_opinion"] = f"{llm['field'] or 'none'} ({int(llm['confidence']*100)}%) - {llm['reason']}"
        return Escalation(id=eid, type="ambiguous_mapping",
                          title=f"Where should column '{m.source_column}' go? ({t.name})",
                          summary=m.note, context=ctx, options=opts, severity="high" if m.status == "ambiguous" else "medium")

    # ----------------------------------------------------------- 3. transform
    def transform(self, t: SourceTable, maps: list[ColumnMapping]) -> list[Record]:
        col_to_field = {m.source_column: m.target for m in maps if m.target}
        # column-level date order inference (one decision per column, not per row)
        slash_order: dict[str, str | None] = {}
        date_cols = [(c, f) for c, f in col_to_field.items() if self.schema.fields[f].type == "date"]
        evidence = {c: C.infer_slash_date_order([r[c] for r in t.rows]) for c, _ in date_cols}
        # one export uses one convention: columns without their own evidence borrow the file's
        file_orders = {o for o, _ in evidence.values() if o}
        file_order = file_orders.pop() if len(file_orders) == 1 else None
        for col, fld in date_cols:
            order, why = evidence[col]
            eid = f"dateorder:{t.name}:{col}"
            d = self.decided(eid)
            needs_order = any(C.clean_date(r[col], None)[1] == 0.3 for r in t.rows if r[col])
            if d:
                order = d["value"]
                self.escalate(Escalation(eid, "date_order", f"Date order for '{col}' ({t.name})", why, {}, []))
            elif order is None and needs_order and file_order:
                order = file_order
                self.emit("clean", f"{t.name}: '{col}' has no disambiguating value; using the file's convention "
                                   f"({'dd/mm/yyyy' if order=='dmy' else 'mm/dd/yyyy'}) inferred from its other date columns", {"file": t.name})
            elif order is None and needs_order:
                self.escalate(Escalation(id=eid, type="date_order",
                    title=f"Is '{col}' in {t.name} dd/mm or mm/dd?", summary=why,
                    context={"file": t.name, "column": col, "sample_values": [r[col] for r in t.rows if r[col]][:6]},
                    options=[{"id": "dmy", "label": "dd/mm/yyyy", "value": "dmy"}, {"id": "mdy", "label": "mm/dd/yyyy", "value": "mdy"}],
                    severity="high"))
            elif order:
                self.emit("clean", f"{t.name}: '{col}' read as {'dd/mm/yyyy' if order=='dmy' else 'mm/dd/yyyy'} - {why}", {"file": t.name})
            slash_order[col] = order

        records = []
        for r in t.rows:
            fields: dict = {}
            changes: list[Change] = []
            issues: list[str] = []
            for col, fld in col_to_field.items():
                raw = r.get(col, "")
                spec = self.schema.fields[fld]
                val, conf, note = self._clean_value(spec, raw, slash_order.get(col), r)
                if fld == "full_name":
                    continue  # handled below
                if val is None and raw != "" and conf < 0.5:
                    issues.append(f"{fld}: {note}")
                    changes.append(Change(fld, raw, None, note, confidence=conf))
                    fields[fld] = None
                    continue
                if val != raw and raw != "":
                    changes.append(Change(fld, raw, val, note, confidence=conf))
                fields[fld] = val
            # derive first/last from full_name when the source lacks them
            fn_col = next((c for c, f in col_to_field.items() if f == "full_name"), None)
            if fn_col and not (fields.get("first_name") and fields.get("last_name")):
                first, last, conf, note = C.split_full_name(r[fn_col])
                if conf >= 0.5:
                    fields["first_name"], fields["last_name"] = first, last
                    changes.append(Change("first_name/last_name", r[fn_col], f"{first} | {last}", note, confidence=conf))
                else:
                    fields.setdefault("first_name", None); fields.setdefault("last_name", None)
                    issues.append(f"name: {note}")
                    changes.append(Change("first_name/last_name", r[fn_col], None, note, confidence=conf))
            fields.pop("full_name", None)
            key = _key_for(fields, t.name, r["_row"])
            records.append(Record(key=key, fields=fields, sources=[{"file": t.name, "row": r["_row"]}], changes=changes, issues=issues))
        self.emit("clean", f"{t.name}: normalised {len(records)} rows ({sum(len(x.changes) for x in records)} value fixes)", {"file": t.name})
        self._pause()
        return records

    def _clean_value(self, spec, raw: str, slash_order, row: dict):
        t = spec.type
        if t == "email":
            return C.clean_email(raw)
        if t == "phone":
            return C.clean_phone(raw)
        if t == "number":
            return C.clean_number(raw)
        if t == "date":
            val, conf, note = C.clean_date(raw, slash_order)
            if val is None and raw and conf == 0.0:
                # second attempt: alternate order, then a lenient parser
                alt = {"dmy": "mdy", "mdy": "dmy"}.get(slash_order)
                v2, c2, n2 = C.clean_date(raw, alt) if alt else (None, 0.0, "")
                if v2 is None:
                    try:
                        from dateutil import parser as dp
                        v2 = dp.parse(raw, dayfirst=(slash_order == "dmy")).date().isoformat(); c2, n2 = 0.7, "lenient parse"
                    except Exception:  # noqa: BLE001
                        v2 = None
                if v2 is not None:
                    return v2, 0.7, f"{note}; retried: {n2}"
                return None, 0.0, f"{note}; retry also failed"
            return val, conf, note
        if t == "enum":
            val, conf, note = C.clean_enum(spec.name, raw, spec.values)
            if val is None and raw:
                remembered = self.memory.enum(spec.name, raw)
                if remembered is not None:
                    return remembered, 1.0, "reused a value mapping a consultant confirmed earlier"
                d = self.decided(f"enum:{spec.name}:{raw.strip().lower()}")
                if d:
                    return (None if d.get("value") in (None, "__blank__") else d["value"]), 1.0, "resolved by consultant"
                if self.llm.available:
                    s = self.llm.suggest_enum(spec.name, raw, spec.values, {k: v for k, v in row.items() if k != "_row"})
                    if s and s["value"] and s["confidence"] >= LLM_TRUST:
                        return s["value"], 0.7, f"LLM suggested '{s['value']}' ({s['reason']})"
                return None, 0.2, note
            return val, conf, note
        if t == "string":
            return C.clean_text(raw, title_case=spec.name in ("first_name", "last_name", "location"))
        return C.clean_text(raw)

    # ------------------------------------------------------- 4. dedupe/merge
    def dedupe_exact(self, recs: list[Record], file: str) -> list[Record]:
        seen: dict[str, Record] = {}
        dropped = 0
        for r in recs:
            sig = _h(repr(sorted((k, v) for k, v in r.fields.items())))
            if sig in seen:
                seen[sig].sources.extend(r.sources); dropped += 1
            else:
                seen[sig] = r
        if dropped:
            self.emit("dedupe", f"{file}: dropped {dropped} exact duplicate row(s)", {"file": file})
        return list(seen.values())

    def merge(self, per_file: list[tuple[str, list[Record]]]) -> list[Record]:
        """Merge the same person across files. Files earlier in the list win soft conflicts."""
        by_key: dict[str, list[tuple[int, Record]]] = {}
        id_to_key: dict[str, str] = {}
        for prio, (file, recs) in enumerate(per_file):
            for r in recs:
                k = r.key
                if not r.fields.get("email") and r.fields.get("employee_id") and r.fields["employee_id"] in id_to_key:
                    k = id_to_key[r.fields["employee_id"]]
                by_key.setdefault(k, []).append((prio, r))
                if r.fields.get("employee_id"):
                    id_to_key.setdefault(r.fields["employee_id"], k)
        merged: list[Record] = []
        conflicts_auto = 0
        for key, group in by_key.items():
            group.sort(key=lambda x: x[0])
            base = Record(key=key, fields={}, sources=[], changes=[], issues=[])
            for _, r in group:
                base.sources += r.sources; base.changes += r.changes; base.issues += r.issues
            for fld in self.schema.pushed_fields:
                vals = [(per_file[p][0], r.fields.get(fld)) for p, r in group if r.fields.get(fld) not in (None, "")]
                distinct = list(dict.fromkeys(v for _, v in vals))
                if not distinct:
                    base.fields[fld] = None
                elif len(distinct) == 1:
                    base.fields[fld] = distinct[0]
                    if len(group) > 1 and any(r.fields.get(fld) in (None, "") for _, r in group):
                        base.changes.append(Change(fld, None, distinct[0], f"filled from {vals[0][0]} (blank in other source)"))
                elif fld in CRITICAL_FIELDS:
                    eid = f"conflict:{key}:{fld}"
                    d = self.decided(eid)
                    base.fields[fld] = d["value"] if d else vals[0][1]
                    e = Escalation(id=eid, type="conflict", severity="high",
                        title=f"{fld} differs between sources for {key}",
                        summary=" vs ".join(f"{v} ({f})" for f, v in vals),
                        context={"record": key, "field": fld, "values": [{"source": f, "value": v} for f, v in vals]},
                        options=[{"id": f"v{i}", "label": f"{v}  (from {f})", "value": v} for i, (f, v) in enumerate(vals)],
                        custom={"label": "Enter the correct value", "fields": [fld]}, affected=[key])
                    self.escalate(e)
                    if not d:
                        base.escalations.append(eid)
                    else:
                        base.changes.append(Change(fld, vals[0][1], d["value"], "conflict resolved by consultant", actor="human"))
                else:
                    base.fields[fld] = vals[0][1]; conflicts_auto += 1
                    base.changes.append(Change(fld, " | ".join(str(v) for _, v in vals[1:]), vals[0][1],
                                               f"sources disagree; kept {vals[0][0]} (highest-priority source)", confidence=0.7))
            merged.append(base)
        self.emit("merge", f"Merged {sum(len(g) for g in by_key.values())} rows into {len(merged)} unique people; "
                           f"{conflicts_auto} soft conflict(s) resolved by source priority", {})
        self._pause()
        return merged

    def fuzzy_duplicates(self, recs: list[Record]) -> list[Record]:
        """Same name + DOB (or same employee_id) but different email -> ask."""
        keep = []
        idx: dict[str, Record] = {}
        for r in sorted(recs, key=lambda x: -len(x.sources)):   # better-evidenced record is the primary
            f = r.fields
            sigs = []
            if f.get("first_name") and f.get("last_name") and f.get("date_of_birth"):
                sigs.append(f"n:{f['first_name'].lower()}|{f['last_name'].lower()}|{f['date_of_birth']}")
            if f.get("employee_id"):
                sigs.append(f"id:{f['employee_id']}")
            primary = next((idx[s] for s in sigs if s in idx), None)
            if primary and primary.key != r.key:
                eid = f"dup:{primary.key}|{r.key}"
                d = self.decided(eid)
                e = Escalation(id=eid, type="possible_duplicate", severity="medium",
                    title=f"Is {r.key} the same person as {primary.key}?",
                    summary=f"Same {'name and date of birth' if sigs[0].startswith('n:') else 'employee_id'}, different email",
                    context={"primary": {"key": primary.key, **{k: primary.fields.get(k) for k in ('employee_id','first_name','last_name','date_of_birth','email','hire_date')}, "sources": primary.sources},
                             "suspect": {"key": r.key, **{k: r.fields.get(k) for k in ('employee_id','first_name','last_name','date_of_birth','email','hire_date')}, "sources": r.sources}},
                    options=[{"id": "merge", "label": f"Same person - merge into {primary.key} and drop the other", "value": "merge"},
                             {"id": "keep", "label": "Different people - keep both", "value": "keep"}],
                    affected=[r.key, primary.key])
                self.escalate(e)
                if d and d.get("value") == "merge":
                    for k, v in r.fields.items():
                        if primary.fields.get(k) in (None, "") and v not in (None, ""):
                            primary.fields[k] = v
                            primary.changes.append(Change(k, None, v, f"filled from duplicate {r.key}", actor="human"))
                    primary.sources += r.sources
                    self.emit("dedupe", f"Merged {r.key} into {primary.key} (consultant confirmed duplicate)", {})
                    continue
                if not d:
                    r.escalations.append(eid)
            for s in sigs:
                idx.setdefault(s, r)
            keep.append(r)
        return keep

    # ------------------------------------------------------------ 5. validate
    def validate(self, recs: list[Record]):
        emails: dict[str, int] = {}
        ids: dict[str, int] = {}
        for r in recs:
            if r.fields.get("email"): emails[r.fields["email"]] = emails.get(r.fields["email"], 0) + 1
            if r.fields.get("employee_id"): ids[r.fields["employee_id"]] = ids.get(r.fields["employee_id"], 0) + 1
        escalated = 0
        for r in recs:
            f = r.fields
            # apply per-record human fixes first
            for fld in self.schema.pushed_fields + ["name"]:
                d = self.decided(f"fix:{r.key}:{fld}")
                if not d: continue
                if d.get("value") == "__reject__":
                    r.status = "rejected"; r.issues.append("rejected by consultant"); break
                if fld == "name":
                    raw = next((c.before for c in r.changes if c.field == "first_name/last_name"), "")
                    vals = d.get("values") or ({"first_name": raw, "last_name": None} if d.get("value") == "__single__" else {})
                else:
                    vals = d.get("values") or {fld: d.get("value")}
                for k, v in vals.items():
                    if k in ("__set_status_active__",) or v == "__set_status_active__": continue
                    before = f.get(k); f[k] = (v or None)
                    if before != f[k]:
                        r.changes.append(Change(k, before, f[k], "corrected by consultant", actor="human"))
            if r.status == "rejected":
                continue
            problems: list[tuple[str, str]] = []   # (field, message)
            for fld in self.schema.required_fields:
                if f.get(fld) in (None, ""):
                    why = next((i for i in r.issues if i.startswith(f"{fld}:") or (fld in ("first_name","last_name") and i.startswith("name:"))), None)
                    problems.append((fld, why or f"{fld} is required but missing in every source"))
            if f.get("email") and emails.get(f["email"], 0) > 1: problems.append(("email", "duplicate email in dataset"))
            if f.get("employee_id") and ids.get(f["employee_id"], 0) > 1 and f.get("email") not in (None, ""):
                pass   # employee_id collisions across different emails are handled by fuzzy_duplicates
            for spec in self.schema.fields.values():
                v = f.get(spec.name)
                if v in (None, ""): continue
                if spec.type == "enum" and v not in spec.values: problems.append((spec.name, f"'{v}' not in {spec.values}"))
                if spec.type == "number" and spec.min is not None and float(v) < spec.min: problems.append((spec.name, f"must be >= {spec.min}"))
            for rule in self.schema.rules:
                if "when" in rule and all(f.get(k) == v for k, v in rule["when"].items()):
                    for req in rule["require"]:
                        if f.get(req) in (None, ""): problems.append((req, rule["description"]))
                if "compare" in rule:
                    c = rule["compare"]; l, rr = f.get(c["left"]), f.get(c["right"])
                    if l and rr:
                        ok = l >= rr if c["op"] == ">=" else l <= rr
                        if ok and c.get("min_years"):
                            ok = int(l[:4]) - int(rr[:4]) >= c["min_years"]
                        if not ok: problems.append((c["left"], rule["description"]))
            # escalate what the agent could not fix (one escalation per root cause)
            seen = set()
            name_problem = [p for p in problems if p[0] in ("first_name", "last_name")]
            if len(name_problem) == 2 and any(i.startswith("name:") for i in r.issues):
                problems = [p for p in problems if p[0] not in ("first_name", "last_name")] + [("name", name_problem[0][1])]
            for fld, msg in problems:
                if fld in seen: continue
                seen.add(fld)
                # unknown enum value -> ONE escalation per distinct raw value, shared by every record carrying it
                raw_enum = next((c for c in r.changes if c.field == fld and c.after is None and "is not a known" in c.note), None)
                if raw_enum is not None:
                    spec = self.schema.fields[fld]
                    eid = f"enum:{fld}:{raw_enum.before.strip().lower()}"
                    if self.decided(eid): continue
                    e = self.escalations.get(eid) or Escalation(id=eid, type="unknown_value", severity="high",
                        title=f"What does {fld} value '{raw_enum.before}' mean?",
                        summary=f"'{raw_enum.before}' matches no allowed {fld} value or known synonym",
                        context={"field": fld, "raw_value": raw_enum.before, "allowed": spec.values, "example_records": []},
                        options=[{"id": v, "label": v, "value": v} for v in spec.values] + [{"id": "__blank__", "label": "Leave blank", "value": "__blank__"}],
                        affected=[])
                    e.affected.append(r.key)
                    e.context["example_records"].append({k: f.get(k) for k in ("employee_id", "first_name", "last_name", "job_title", "email")})
                    e.summary = f"'{raw_enum.before}' matches no allowed {fld} value or known synonym; {len(e.affected)} record(s) affected"
                    self.escalate(e); r.escalations.append(eid); escalated += 1
                    continue
                if fld == "name":
                    eid = f"fix:{r.key}:name"
                    if self.decided(eid): continue
                    raw = next((c.before for c in r.changes if c.field == "first_name/last_name"), "")
                    e = Escalation(id=eid, type="validation_failed", severity="high",
                        title=f"Split the name '{raw}' for {r.key}", summary=msg,
                        context={"record": r.key, "raw_values": [{"before": raw, "note": msg}],
                                 "record_preview": {k: f.get(k) for k in ("employee_id","email","job_title","department","hire_date")}, "sources": r.sources},
                        options=[{"id": "__single__", "label": f"Use '{raw}' as first name, leave last name blank", "value": "__single__"},
                                 {"id": "__reject__", "label": "Reject this record (do not migrate)", "value": "__reject__"}],
                        custom={"label": "Enter the name parts", "fields": ["first_name", "last_name"]}, affected=[r.key])
                    self.escalate(e); r.escalations.append(eid); escalated += 1
                    continue
                eid = f"fix:{r.key}:{fld}"
                if self.decided(eid): continue
                spec = self.schema.fields[fld]
                opts = []
                if spec.type == "enum":
                    opts = [{"id": v, "label": v, "value": v} for v in spec.values]
                if fld == "termination_date" and f.get("status") == "terminated":
                    opts.append({"id": "__active__", "label": "Actually still active - set status to active", "value": "__set_status_active__"})
                opts.append({"id": "__reject__", "label": "Reject this record (do not migrate)", "value": "__reject__"})
                raw_evidence = [c for c in r.changes if c.field == fld]
                e = Escalation(id=eid, type="validation_failed", severity="high" if spec.required else "medium",
                    title=f"{fld} for {r.key}: {msg}",
                    summary=msg,
                    context={"record": r.key, "field": fld, "raw_values": [{"before": c.before, "note": c.note} for c in raw_evidence],
                             "record_preview": {k: f.get(k) for k in ("employee_id","first_name","last_name","email","hire_date","department","status","termination_date") },
                             "sources": r.sources},
                    options=opts, custom={"label": f"Enter {fld}", "fields": [fld]}, affected=[r.key])
                self.escalate(e); r.escalations.append(eid); escalated += 1
            # a "set status active" decision arrives on the termination_date escalation
            d = self.decided(f"fix:{r.key}:termination_date")
            if d and d.get("value") == "__set_status_active__":
                f["termination_date"] = None
                if f.get("status") == "terminated":
                    f["status"] = "active"; r.changes.append(Change("status", "terminated", "active", "consultant: still active", actor="human"))
        self.emit("validate", f"Validated {len(recs)} records; {escalated} need a human decision", {})
        self._pause()

    # ---------------------------------------------------------------- run all
    def run(self, paths: list[str]) -> RunResult:
        self.emit("start", f"Agent run started on {len(paths)} file(s). LLM: {self.llm.detail}", {})
        tables = self.ingest(paths)
        mappings: dict[str, list[dict]] = {}
        per_file: list[tuple[str, list[Record]]] = []
        for t in tables:
            self.emit("phase", f"Mapping columns of {t.name}", {"file": t.name})
            maps = self.map_columns(t)
            mappings[t.name] = [{"source_column": m.source_column, "target": m.target, "confidence": m.confidence,
                                 "status": m.status, "note": m.note, "samples": m.samples,
                                 "candidates": [asdict(c) for c in m.candidates]} for m in maps]
            recs = self.transform(t, maps)
            recs = self.dedupe_exact(recs, t.name)
            per_file.append((t.name, recs))
        self.emit("phase", "Reconciling people across files", {})
        merged = self.merge(per_file)
        merged = self.fuzzy_duplicates(merged)
        self.emit("phase", "Validating against target schema", {})
        self.validate(merged)
        for r in merged:
            r.escalations = [e for e in r.escalations if self.escalations.get(e) and self.escalations[e].status == "open"]
            if r.status != "rejected":
                r.status = "held" if r.escalations else "ready"
        open_e = [e for e in self.escalations.values() if e.status == "open"]
        stats = {"source_rows": sum(len(t.rows) for t in tables), "unique_people": len(merged),
                 "ready": sum(r.status == "ready" for r in merged), "held": sum(r.status == "held" for r in merged),
                 "rejected": sum(r.status == "rejected" for r in merged),
                 "auto_fixes": sum(1 for r in merged for c in r.changes if c.actor != "human"),
                 "open_escalations": len(open_e), "resolved_escalations": len(self.escalations) - len(open_e)}
        self.emit("done", f"Run complete: {stats['unique_people']} people, {stats['ready']} ready to push, "
                          f"{stats['held']} held on {len(open_e)} open escalation(s), {stats['auto_fixes']} fixes applied autonomously", stats)
        files = [{"name": t.name, "rows": len(t.rows), "columns": t.columns} for t in tables]
        return RunResult(files=files, mappings=mappings, records=merged, escalations=list(self.escalations.values()), stats=stats)
