#!/usr/bin/env bash
# One-shot local run. Requires python3.11+.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements-dev.txt
[ -f sample_data/legacy_hris_employees.csv ] || python scripts/make_sample_data.py
echo "→ open http://localhost:8000"
exec uvicorn app.main:app --port 8000
