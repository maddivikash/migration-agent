# Write-up: scoping the agent's autonomy

Live demo: <https://migration-agent-sage.vercel.app> · Code: <https://github.com/maddivikash/migration-agent>

## Approach

I treated this as an autonomy-scoping problem, not a parsing problem. The pipeline itself is
deliberately boring: ingest → propose column mappings → clean values → merge people across
files → validate → push. What matters is the **confidence contract** shared by every step.
Every cleaner and every mapping decision returns a value plus a confidence: **1.0** means the
evidence is unambiguous, apply silently and log; **0.7** means apply but flag it in the audit
trail as a judgement call; **below 0.5** means do not guess, escalate. The agent is
deterministic-first. An open-source LLM (Qwen2.5 via Ollama, optional) is consulted only for
tie-breaks, and its suggestion must both agree with the heuristic and clear 0.85 confidence
before the agent acts on it alone. Otherwise the human sees the model's opinion as context on
the escalation card, not as a decision.

The whole run is a pure function of (source files, target schema, human decisions). Resolving
an escalation appends a decision and re-runs in milliseconds, so the UI never has half-applied
state and every audit entry is reproducible.

## Where I drew the line

The agent **handles alone** anything that is mechanical, reversible from the audit trail, and
where a wrong guess costs little: whitespace and casing; phone formatting; enum synonyms
(`FT` → `full_time`, `Y` → `active`, `ENG` → `Engineering`); date parsing when the format is
unambiguous, or when the *column* disambiguates dd/mm vs mm/dd (one decision per column, never
per row, and a column with no evidence borrows the convention of the other date columns in the
same file); exact duplicate rows; filling a blank from another source; conflicts on soft fields
(keep the system-of-record value, log the loser); columns that fit no target (drop, but list them
so the consultant can rescue one from the mapping table); three-token names (first + rest, flagged).

It **escalates** when a wrong guess would corrupt identity or be hard to undo *and* the evidence
does not favour one answer: a column that fits two target fields about equally (e.g. `Personal
Email` vs `email`/`manager_email`); a date order it cannot infer; a value that fails cleaning twice
(first pass, then an alternate strategy: `31/02/2020`); an enum value with no synonym match and no
confident model suggestion (`BD`), asked once per distinct value, not per row; conflicting values
across sources on identity-critical fields (`hire_date`, `date_of_birth`, `employee_id`, `email`); a
probable duplicate that is not exact (same name and DOB, different email); a business-rule violation
the agent cannot fix (terminated with no termination date); a single-token full name.

The test is: *would a good implementation consultant be annoyed to be asked this, or annoyed not
to be?* On the sample data, 115 source rows and 41 columns produce 804 autonomous fixes and 7
questions. The agent also refuses to escalate things that only look like problems: an unknown
department code on a payroll row is not raised if the HR file already gives that person a
department, and records whose manager is still held are deferred at push time rather than failed.

## Integration and audit

Push goes managers-first so the target's referential check passes, the upsert is idempotent so a
retry after a 503 is safe, 422s are shown as permanent with a targeted fix ("retry without
manager"), and rollback deletes in reverse order. Every value change records before, after, reason,
actor and confidence; every target call records payload, HTTP status and outcome.

## Delta on top of the AI

Consultant decisions are remembered. A mapping or enum answer given once is reused on the next run
and on the next client migrating from the same legacy tool, so the agent gets quieter with use
rather than asking the same question each time.

## What I would build next

1. **Dry-run diff against the live target** before push, so the consultant approves a change set,
   not just a record set, and re-migrations become idempotent updates.
2. **Confidence calibration from outcomes**: track how often 0.7-confidence fixes are later
   corrected, and move the auto/flag/escalate thresholds per field from that data.
3. **Multi-entity migration** (departments, locations, managers as first-class entities) with
   dependency-ordered pushes and cross-entity referential validation.
4. **Escalation batching**: group questions by root cause across entities ("these 14 rows all come
   from one bad export column") and let the consultant answer in bulk.
5. **Real target connectors** (Darwinbox APIs) with per-tenant rate limits, plus checkpoint/resume
   for datasets in the hundreds of thousands of rows.
