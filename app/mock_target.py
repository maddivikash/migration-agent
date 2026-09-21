"""Stub of the *new* HR platform's API. Mounted at /target inside the same server.

Behaviour deliberately mirrors a real integration:
  - PUT /target/employees/{employee_id} is an idempotent upsert (safe to retry)
  - DELETE /target/employees/{employee_id} removes a record (used for rollback)
  - 422 if the payload violates the target's own rules (manager must already exist,
    salary must be within the plan's limit) - permanent, retrying won't help
  - 503 "rate limited" on the first attempt for a deterministic subset of records -
    transient, a retry succeeds. Lets the demo show retry without randomness.
"""
from __future__ import annotations
import hashlib, time
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/target", tags=["mock-target"])
STORE: dict[str, dict] = {}
ATTEMPTS: dict[str, int] = {}
SALARY_LIMIT = 5_000_000


def _chaos(emp_id: str) -> bool:
    return int(hashlib.sha1(emp_id.encode()).hexdigest(), 16) % 9 == 0


@router.get("/employees")
def list_employees():
    return {"count": len(STORE), "employees": list(STORE.values())}


@router.put("/employees/{employee_id}")
async def upsert(employee_id: str, req: Request):
    body = await req.json()
    ATTEMPTS[employee_id] = ATTEMPTS.get(employee_id, 0) + 1
    if _chaos(employee_id) and ATTEMPTS[employee_id] == 1:
        raise HTTPException(503, "rate limited - retry after 1s")
    if body.get("manager_email") and not any(e.get("email") == body["manager_email"] for e in STORE.values()):
        raise HTTPException(422, f"manager_email {body['manager_email']} does not exist in the target system")
    if body.get("salary") is not None and float(body["salary"]) > SALARY_LIMIT:
        raise HTTPException(422, f"salary exceeds the plan limit of {SALARY_LIMIT:,}")
    created = employee_id not in STORE
    STORE[employee_id] = {**body, "employee_id": employee_id, "_updated_at": time.time()}
    return {"status": "created" if created else "updated", "employee_id": employee_id}


@router.delete("/employees/{employee_id}")
def delete(employee_id: str):
    if employee_id not in STORE:
        raise HTTPException(404, "not found")
    del STORE[employee_id]
    return {"status": "deleted", "employee_id": employee_id}


@router.post("/reset")
def reset():
    STORE.clear(); ATTEMPTS.clear()
    return {"status": "reset"}
