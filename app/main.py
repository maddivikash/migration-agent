"""FastAPI app: agent orchestration + human-in-the-loop API + mock target + static UI."""
from __future__ import annotations
import csv, io, json, os, shutil, threading, time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import httpx
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.schema import load_schema
from app.agent.memory import Memory
from app.agent.llm import LLM
from app.agent.runner import Agent, RunResult
from app import mock_target

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(os.getenv("MIGRATION_SCHEMA", ROOT / "target_schema.yaml"))
DATA_DIR = Path(os.getenv("MIGRATION_DATA", ROOT / "sample_data"))
ON_VERCEL = bool(os.getenv("VERCEL"))
_WRITABLE = Path("/tmp/migration-agent") if ON_VERCEL else ROOT
UPLOAD_DIR = _WRITABLE / ".uploads"
STATE_DIR = _WRITABLE / ".state"
# Locally the run happens in a background thread with a small delay per step so the live feed is
# watchable. On serverless there is no background thread: the run is synchronous (< 1 s) and the
# UI replays the events with a stagger instead.
SYNC_RUN = ON_VERCEL or os.getenv("MIGRATION_SYNC_RUN") == "1"
STEP_DELAY = 0.0 if SYNC_RUN else float(os.getenv("MIGRATION_STEP_DELAY", "0.35"))
REPO_URL = os.getenv("MIGRATION_REPO_URL", "https://github.com/maddivikash/migration-agent")

app = FastAPI(title="Migration Agent", version="0.1")
app.include_router(mock_target.router)

schema = load_schema(SCHEMA_PATH)
memory = Memory(STATE_DIR / "mapping_memory.json")
llm = LLM()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"                 # idle | running | done | error
        self.files: list[str] = []
        self.events: list[dict] = []
        self.result: RunResult | None = None
        self.decisions: dict[str, dict] = {}
        self.decision_log: list[dict] = []   # who decided what, when, on which escalation
        self.push_log: list[dict] = []       # every call to the target, with outcome
        self.record_status: dict[str, dict] = {}   # key -> {status, attempts, last_error, pushed_payload}
        self.run_no = 0
        self.version = 0      # bumped on every mutation; lets a client tell a stale instance apart
        self.epoch = 0        # bumped when a run resets everything; tells the UI to clear its feed

    def emit(self, kind: str, message: str, details: dict):
        with self.lock:
            self.events.append({"seq": len(self.events) + 1, "ts": now(), "kind": kind, "message": message, "details": details})


S = State()


def bump():
    S.version += 1


def _paths(names: list[str]) -> list[str]:
    return [str(UPLOAD_DIR / n if (UPLOAD_DIR / n).exists() else DATA_DIR / n) for n in names]


# ---------------------------------------------------------------- session sync (serverless safety)
# The browser holds a copy of the whole session. Every request carries the version it last saw; an
# instance that is behind (fresh cold start, or a sibling instance that never saw this run) asks for the
# full session, adopts it and rebuilds the derived state. A run is a pure function of
# (files, decisions), so rebuilding is exact - no half-applied state can leak in.
def export_session() -> dict:
    return {"v": S.version, "epoch": S.epoch, "files": S.files, "run_no": S.run_no, "events": S.events,
            "decisions": S.decisions, "decision_log": S.decision_log, "push_log": S.push_log,
            "record_status": S.record_status,
            "target": {"store": mock_target.STORE, "attempts": mock_target.ATTEMPTS}, "memory": memory.data}


def _rebuild():
    """Recompute the derived state (records, escalations) from files + decisions, without emitting events."""
    if not S.files:
        S.result = None; S.status = "idle"; return
    ag = Agent(schema, memory, llm, lambda *a: None, dict(S.decisions), step_delay=0.0)
    res = ag.run(_paths(S.files))
    _merge_push_status(res)
    S.result = res; S.status = "done"


def hydrate(sess: dict | None):
    if not sess or sess.get("v", 0) <= S.version or S.status == "running":
        return False
    with S.lock:
        S.files = list(sess.get("files", [])); S.run_no = sess.get("run_no", 0)
        S.events = list(sess.get("events", [])); S.decisions = dict(sess.get("decisions", {}))
        S.decision_log = list(sess.get("decision_log", [])); S.push_log = list(sess.get("push_log", []))
        S.record_status = dict(sess.get("record_status", {}))
        S.version, S.epoch = sess["v"], sess.get("epoch", 0)
        tgt = sess.get("target", {})
        mock_target.STORE.clear(); mock_target.STORE.update(tgt.get("store", {}))
        mock_target.ATTEMPTS.clear(); mock_target.ATTEMPTS.update(tgt.get("attempts", {}))
        if sess.get("memory"):
            memory.data = sess["memory"]
    if S.run_no:
        _rebuild()
    else:
        S.result = None; S.status = "idle"
    return True


def stale(v: int | None) -> bool:
    """True when the client has seen a newer version than this instance holds."""
    return v is not None and v > S.version and S.status != "running"


class SessionBody(BaseModel):
    v: int | None = None
    session: dict | None = None


def _merge_push_status(res: RunResult):
    for r in res.records:
        st = S.record_status.get(r.key)
        if st and st["status"] in ("pushed", "failed", "rolled_back"):
            r.status, r.push = st["status"], {k: v for k, v in st.items() if k != "status"}


def _agent(delay: float) -> Agent:
    return Agent(schema, memory, llm, S.emit, dict(S.decisions), step_delay=delay)


def _run(files: list[str], delay: float):
    S.status = "running"
    S.run_no += 1
    try:
        res = _agent(delay).run(files)
        with S.lock:
            _merge_push_status(res)   # keep push status for records that already went out
            S.result = res
            S.status = "done"
            bump()
    except Exception as e:  # noqa: BLE001
        S.status = "error"
        S.emit("error", f"Agent crashed: {type(e).__name__}: {e}", {})
        raise


def default_file_order() -> list[str]:
    """Priority order from sources.yaml, then any other data files alphabetically, then uploads."""
    import yaml
    listed = []
    if (ROOT / "sources.yaml").exists():
        listed = yaml.safe_load((ROOT / "sources.yaml").read_text()).get("files", [])
    present = {p.name for p in DATA_DIR.iterdir() if p.suffix.lower() in (".csv", ".xlsx", ".xls")}
    uploads = sorted(p.name for p in UPLOAD_DIR.iterdir() if p.suffix.lower() in (".csv", ".xlsx", ".xls")) if UPLOAD_DIR.exists() else []
    order = [n for n in listed if n in present] + sorted(present - set(listed)) + [u for u in uploads if u not in listed]
    return order


# ------------------------------------------------------------------ run control
class RunRequest(SessionBody):
    files: list[str] | None = None        # names inside sample_data / uploads; default = all sample files
    reset: bool = True


@app.post("/api/run")
def start_run(req: RunRequest):
    if S.status == "running":
        raise HTTPException(409, "a run is already in progress")
    if stale(req.v) and not req.session:
        return {"need_session": True}
    hydrate(req.session)
    if req.reset:
        v, ep = S.version, S.epoch
        S.__init__()
        S.version, S.epoch = v, ep + 1
        mock_target.reset()
        memory.data = {"column_mappings": memory.data.get("column_mappings", {}), "enum_values": memory.data.get("enum_values", {})}
    llm.probe()
    names = req.files or default_file_order()
    paths = []
    for n in names:
        p = UPLOAD_DIR / n if (UPLOAD_DIR / n).exists() else DATA_DIR / n
        if not p.exists():
            raise HTTPException(404, f"file not found: {n}")
        paths.append(str(p))
    S.files = [Path(p).name for p in paths]
    if SYNC_RUN:
        _run(paths, 0.0)
        return {"status": "done", "files": S.files, "session": export_session()}
    threading.Thread(target=_run, args=(paths, STEP_DELAY), daemon=True).start()
    return {"status": "started", "files": S.files}


@app.post("/api/upload")
async def upload(file: UploadFile):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / Path(file.filename).name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"name": dest.name}


@app.get("/api/files")
def list_files():
    names = {p.name for p in DATA_DIR.iterdir() if p.suffix.lower() in (".csv", ".xlsx", ".xls")}
    if UPLOAD_DIR.exists():
        names |= {p.name for p in UPLOAD_DIR.iterdir() if p.suffix.lower() in (".csv", ".xlsx", ".xls")}
    return {"files": sorted(names)}


@app.get("/api/state")
def get_state():
    res = S.result
    stats = dict(res.stats) if res else {}
    if res:
        counts = {}
        for r in res.records:
            counts[r.status] = counts.get(r.status, 0) + 1
        stats["by_status"] = counts
        stats["open_escalations"] = sum(1 for e in res.escalations if e.status == "open")
    return {"status": S.status, "files": S.files, "run_no": S.run_no, "v": S.version, "epoch": S.epoch, "stats": stats,
            "llm": {"available": llm.available, "detail": llm.detail, "model": llm.model},
            "memory": memory.summary(), "schema": {"entity": schema.entity, "fields": schema.pushed_fields},
            "target_count": len(mock_target.STORE), "events": len(S.events),
            "deployment": {"serverless": ON_VERCEL, "sync_run": SYNC_RUN, "repo": REPO_URL, "target": TARGET_BASE}}


@app.get("/api/events")
def get_events(since: int = 0, limit: int = 500):
    with S.lock:
        evs = [e for e in S.events if e["seq"] > since][:limit]
    return {"events": evs, "status": S.status}


class SyncRequest(SessionBody):
    since: int = 0


@app.post("/api/sync")
def sync(req: SyncRequest):
    """Everything the UI needs in one round trip. Also the hydration point for a stale instance."""
    if stale(req.v) and not req.session:
        return {"need_session": True, "v": S.version}
    hydrate(req.session)
    with S.lock:
        events = [e for e in S.events if e["seq"] > req.since]
    out = {"state": get_state(), "events": events, "event_count": len(S.events)}
    if S.status != "running":
        out["escalations"] = get_escalations()
        out["mappings"] = S.result.mappings if S.result else {}
        out["records"] = get_records()["records"]
    if req.v is None or req.v != S.version:
        out["session"] = export_session()   # client is behind: hand it the authoritative copy
    return out


@app.get("/api/schema")
def get_schema():
    return {"entity": schema.entity, "identity": schema.identity_keys,
            "fields": {n: asdict(f) for n, f in schema.fields.items()}, "rules": schema.rules}


# ------------------------------------------------------------------ mappings / records
@app.get("/api/mappings")
def get_mappings():
    if not S.result:
        return {"mappings": {}}
    return {"mappings": S.result.mappings}


@app.get("/api/records")
def get_records(status: str | None = None, q: str | None = None):
    if not S.result:
        return {"records": []}
    out = []
    for r in S.result.records:
        if status and r.status != status:
            continue
        if q and q.lower() not in json.dumps(r.fields).lower():
            continue
        out.append({"key": r.key, "status": r.status, "fields": r.fields, "sources": r.sources,
                    "changes": len(r.changes), "human_changes": sum(c.actor == "human" for c in r.changes),
                    "escalations": r.escalations, "push": r.push})
    return {"records": out}


@app.get("/api/records/{key}")
def get_record(key: str):
    r = next((r for r in (S.result.records if S.result else []) if r.key == key), None)
    if not r:
        raise HTTPException(404, "record not found")
    return {"key": r.key, "status": r.status, "fields": r.fields, "sources": r.sources,
            "changes": [asdict(c) for c in r.changes], "issues": r.issues, "escalations": r.escalations, "push": r.push,
            "push_log": [p for p in S.push_log if p["key"] == r.key]}


# ------------------------------------------------------------------ escalations
@app.get("/api/escalations")
def get_escalations():
    if not S.result:
        return {"open": [], "resolved": []}
    open_, resolved = [], []
    seen = set()
    for e in S.result.escalations:
        d = asdict(e); seen.add(e.id)
        (open_ if e.status == "open" else resolved).append(d)
    # escalations resolved in a way that made them disappear from the re-run (e.g. record rejected)
    for log in S.decision_log:
        if log["escalation"]["id"] not in seen:
            resolved.append({**log["escalation"], "status": "resolved", "resolution": log["decision"]})
    for d in resolved:
        log = next((l for l in S.decision_log if l["escalation"]["id"] == d["id"]), None)
        if log:
            d["resolution"] = log["decision"]; d["decided_at"] = log["ts"]
    sev = {"high": 0, "medium": 1, "low": 2}
    open_.sort(key=lambda e: (sev.get(e["severity"], 3), e["type"]))
    return {"open": open_, "resolved": resolved}


class Decision(SessionBody):
    value: Any = None                    # option value
    values: dict[str, Any] | None = None # free-text multi-field correction
    note: str = ""
    remember: bool = True                # save mapping/enum choices to memory for future runs


@app.post("/api/escalations/{eid:path}/decide")
def decide(eid: str, d: Decision):
    if S.status == "running":
        raise HTTPException(409, "wait for the current run to finish")
    if stale(d.v) and not d.session:
        return {"need_session": True}
    hydrate(d.session)
    if not S.result:
        raise HTTPException(400, "no run yet")
    e = next((e for e in S.result.escalations if e.id == eid), None)
    if not e:
        raise HTTPException(404, "escalation not found")
    decision = {"value": d.value, "values": d.values, "note": d.note, "by": "consultant", "ts": now()}
    S.decisions[eid] = decision
    S.decision_log.append({"ts": decision["ts"], "escalation": asdict(e), "decision": decision})
    # learn for next time (the delta on top of the AI)
    if d.remember:
        if e.type == "ambiguous_mapping":
            memory.remember_column(e.context["column"], None if d.value == "__drop__" else d.value)
        elif e.type == "unknown_value" and d.value not in (None, "__blank__"):
            memory.remember_enum(e.context["field"], e.context["raw_value"], d.value)
    S.emit("human", f"Consultant resolved: {e.title} -> {d.values or d.value}" + (f' ("{d.note}")' if d.note else ""),
           {"id": eid, "decision": decision})
    # re-run instantly (no step delay) so the new state is a pure function of decisions
    _run(_paths(S.files), 0.0)
    return {"status": "ok", "state": get_state(), "session": export_session()}


class Override(SessionBody):
    file: str
    column: str
    target: str | None       # None = drop


@app.post("/api/mappings/override")
def override_mapping(o: Override):
    """Consultant changes a mapping the agent made confidently. Treated exactly like a decision."""
    if S.status == "running":
        raise HTTPException(409, "wait for the current run to finish")
    if stale(o.v) and not o.session:
        return {"need_session": True}
    hydrate(o.session)
    eid = f"map:{o.file}:{o.column}"
    decision = {"value": o.target or "__drop__", "values": None, "note": "manual override", "by": "consultant", "ts": now()}
    S.decisions[eid] = decision
    S.decision_log.append({"ts": decision["ts"], "escalation": {"id": eid, "type": "ambiguous_mapping",
                           "title": f"Override: '{o.column}' in {o.file}", "summary": "consultant override of an automatic mapping",
                           "context": {"file": o.file, "column": o.column}, "options": [], "severity": "low", "affected": []},
                           "decision": decision})
    memory.remember_column(o.column, o.target)
    S.emit("human", f"Consultant overrode mapping: {o.file} '{o.column}' -> {o.target or 'DROP'}", {"id": eid})
    _run(_paths(S.files), 0.0)
    return {"status": "ok", "session": export_session()}


# ------------------------------------------------------------------ push / retry / rollback
# "internal" calls the stub in-process (needed on serverless, where a self-HTTP call may land on another
# instance with its own store). Locally the default is a real HTTP hop, so swapping in the client's real
# API is a one-line change.
TARGET_BASE = os.getenv("MIGRATION_TARGET_URL", "internal" if ON_VERCEL else "http://127.0.0.1:8000/target")


class TargetClient:
    """Uniform (status_code, json) interface over the stub, via HTTP or in-process."""
    def __init__(self, base: str):
        self.base = base
        self.http = None if base == "internal" else httpx.Client(timeout=10)

    def put(self, emp_id: str, payload: dict) -> tuple[int, dict]:
        if self.http is None:
            return mock_target.upsert_record(emp_id, payload)
        r = self.http.put(f"{self.base}/employees/{emp_id}", json=payload)
        return r.status_code, (r.json() if r.content else {})

    def delete(self, emp_id: str) -> tuple[int, dict]:
        if self.http is None:
            return mock_target.delete_record(emp_id)
        r = self.http.delete(f"{self.base}/employees/{emp_id}")
        return r.status_code, (r.json() if r.content else {})

    def close(self):
        if self.http: self.http.close()


def _payload(r) -> dict:
    return {k: r.fields.get(k) for k in schema.pushed_fields}


def _ordered(records):
    """Managers before their reports so the target's referential check passes."""
    by_email = {r.fields.get("email"): r for r in records}
    done, out = set(), []
    def visit(r, stack=()):
        if r.key in done or r.key in stack: return
        m = r.fields.get("manager_email")
        if m and m in by_email: visit(by_email[m], stack + (r.key,))
        done.add(r.key); out.append(r)
    for r in records: visit(r)
    return out


class PushRequest(SessionBody):
    keys: list[str] | None = None   # default: every 'ready' record
    mode: str = "push"              # push | retry | retry_without_manager


@app.post("/api/push")
def push(req: PushRequest):
    if stale(req.v) and not req.session:
        return {"need_session": True}
    hydrate(req.session)
    if not S.result or S.status == "running":
        raise HTTPException(409, "no finished run")
    want = set(req.keys or [])
    if req.mode == "push":
        recs = [r for r in S.result.records if r.status == "ready" and (not want or r.key in want)]
    else:
        recs = [r for r in S.result.records if r.status == "failed" and (not want or r.key in want)]
    # defer anyone whose manager is not yet in the target and is still held/failed here:
    # pushing them would fail the target's referential check for a reason the consultant is already handling
    by_email = {r.fields.get("email"): r for r in S.result.records}
    deferred = []
    if req.mode != "retry_without_manager":
        keep = []
        for r in recs:
            m = by_email.get(r.fields.get("manager_email"))
            if m and m.status in ("held", "failed", "rejected", "rolled_back") and m not in recs:
                deferred.append((r, m))
            else:
                keep.append(r)
        recs = keep
    recs = _ordered(recs)
    S.emit("push", f"{'Retrying' if req.mode != 'push' else 'Pushing'} {len(recs)} record(s) to target ({TARGET_BASE})", {"mode": req.mode})
    for r, m in deferred:
        S.emit("push", f"DEFER {r.fields.get('employee_id')} ({r.fields.get('first_name')} {r.fields.get('last_name')}): "
                       f"manager {m.key} is {m.status}; will push once the manager is in the target", {"key": r.key, "manager": m.key})
    ok = fail = 0
    client = TargetClient(TARGET_BASE)
    try:
        for r in recs:
            payload = _payload(r)
            if req.mode == "retry_without_manager" and r.push and "manager_email" in (r.push.get("last_error") or ""):
                r.changes.append(type(r.changes[0])("manager_email", payload["manager_email"], None,
                                 "manager not present in target; cleared on consultant's instruction", actor="human"))
                payload["manager_email"] = None; r.fields["manager_email"] = None
            emp_id = payload.get("employee_id") or r.key
            entry = {"ts": now(), "key": r.key, "employee_id": emp_id, "action": "upsert", "payload": payload}
            try:
                code, body = client.put(emp_id, payload)
                entry["http"] = code
                if code < 300:
                    r.status = "pushed"; ok += 1
                    r.push = {"attempts": (r.push or {}).get("attempts", 0) + 1, "last_error": None, "employee_id": emp_id, "result": body.get("status")}
                    entry["outcome"] = "success"
                else:
                    r.status = "failed"; fail += 1
                    err = body.get("detail", str(body))
                    r.push = {"attempts": (r.push or {}).get("attempts", 0) + 1, "last_error": err, "employee_id": emp_id,
                              "retryable": code >= 500}
                    entry["outcome"], entry["error"] = "failed", err
            except httpx.HTTPError as e:
                r.status = "failed"; fail += 1
                r.push = {"attempts": (r.push or {}).get("attempts", 0) + 1, "last_error": str(e), "employee_id": emp_id, "retryable": True}
                entry["outcome"], entry["error"] = "failed", str(e)
            S.record_status[r.key] = {"status": r.status, **r.push}
            S.push_log.append(entry)
            S.emit("push", f"{'OK ' if entry['outcome']=='success' else 'FAIL'} {emp_id} ({r.fields.get('first_name')} {r.fields.get('last_name')})"
                           + (f": {entry.get('error')}" if entry.get("error") else ""), entry)
    finally:
        client.close()
    S.emit("push", f"Push finished: {ok} succeeded, {fail} failed, {len(deferred)} deferred (manager not yet migrated)", {"ok": ok, "failed": fail, "deferred": len(deferred)})
    bump()
    return {"ok": ok, "failed": fail, "deferred": len(deferred), "session": export_session()}


class RollbackRequest(SessionBody):
    keys: list[str] | None = None


@app.post("/api/rollback")
def rollback(req: RollbackRequest):
    if stale(req.v) and not req.session:
        return {"need_session": True}
    hydrate(req.session)
    if not S.result:
        raise HTTPException(409, "no run")
    want = set(req.keys or [])
    recs = [r for r in S.result.records if r.status == "pushed" and (not want or r.key in want)]
    # reverse dependency order: reports before managers
    recs = list(reversed(_ordered(recs)))
    S.emit("rollback", f"Rolling back {len(recs)} record(s) from target", {})
    n = 0
    client = TargetClient(TARGET_BASE)
    try:
        for r in recs:
            emp_id = r.push["employee_id"]
            code, _ = client.delete(emp_id)
            entry = {"ts": now(), "key": r.key, "employee_id": emp_id, "action": "delete", "http": code,
                     "outcome": "success" if code < 300 or code == 404 else "failed"}
            if entry["outcome"] == "success":
                r.status = "rolled_back"; n += 1
                r.push = {**r.push, "rolled_back_at": entry["ts"]}
                S.record_status[r.key] = {"status": "rolled_back", **r.push}
            S.push_log.append(entry)
            S.emit("rollback", f"Removed {emp_id} from target", entry)
    finally:
        client.close()
    S.emit("rollback", f"Rollback finished: {n} record(s) removed", {"count": n})
    bump()
    return {"rolled_back": n, "session": export_session()}


@app.post("/api/records/{key}/reset")
def reset_record(key: str, body: SessionBody | None = None):
    """Put a rolled-back / failed record back into the ready queue."""
    body = body or SessionBody()
    if stale(body.v) and not body.session:
        return {"need_session": True}
    hydrate(body.session)
    r = next((r for r in (S.result.records if S.result else []) if r.key == key), None)
    if not r:
        raise HTTPException(404)
    r.status = "ready"; r.push = None; S.record_status.pop(key, None)
    bump()
    return {"status": "ready", "session": export_session()}


# ------------------------------------------------------------------ audit
@app.get("/api/audit")
def audit():
    if not S.result:
        return {"events": S.events, "decisions": S.decision_log, "push_log": S.push_log, "records": []}
    return {"generated_at": now(), "files": S.files, "stats": get_state()["stats"], "llm": llm.detail,
            "events": S.events, "decisions": S.decision_log, "push_log": S.push_log,
            "mappings": S.result.mappings,
            "records": [{"key": r.key, "status": r.status, "sources": r.sources, "final": r.fields,
                         "changes": [asdict(c) for c in r.changes], "issues": r.issues} for r in S.result.records]}


@app.get("/api/audit.csv")
def audit_csv():
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["record", "field", "before", "after", "note", "actor", "confidence", "sources"])
    for r in (S.result.records if S.result else []):
        for c in r.changes:
            w.writerow([r.key, c.field, c.before, c.after, c.note, c.actor, c.confidence, "; ".join(f"{s['file']}#{s['row']}" for s in r.sources)])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=migration_audit.csv"})


@app.get("/api/memory")
def get_memory():
    return memory.data


@app.delete("/api/memory")
def clear_memory():
    memory.data = {"column_mappings": {}, "enum_values": {}}; memory.save()
    bump()
    return {"status": "cleared", "session": export_session()}


# ------------------------------------------------------------------ UI
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
