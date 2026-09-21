"""Mapping memory: human decisions persist across runs (and across clients with
the same legacy tool). This is the 'delta' on top of the AI - the agent gets
less chatty every time a consultant corrects it, and the next client on the
same source system starts from a warm cache."""
from __future__ import annotations
import json
from pathlib import Path


class Memory:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"column_mappings": {}, "enum_values": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    # column name (normalised) -> target field or None (=drop)
    def column(self, col: str):
        return self.data["column_mappings"].get(col.strip().lower())

    def remember_column(self, col: str, target: str | None):
        self.data["column_mappings"][col.strip().lower()] = target; self.save()

    def enum(self, field: str, raw: str):
        return self.data["enum_values"].get(field, {}).get(raw.strip().lower())

    def remember_enum(self, field: str, raw: str, canon: str | None):
        self.data["enum_values"].setdefault(field, {})[raw.strip().lower()] = canon; self.save()

    def summary(self) -> dict:
        return {"column_mappings": len(self.data["column_mappings"]),
                "enum_values": sum(len(v) for v in self.data["enum_values"].values())}
