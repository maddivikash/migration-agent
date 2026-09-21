"""Load and query the target schema (YAML)."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml


@dataclass
class FieldSpec:
    name: str
    type: str
    required: bool = False
    unique: bool = False
    virtual: bool = False
    values: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    min: float | None = None


@dataclass
class Schema:
    entity: str
    identity_keys: list[str]
    fields: dict[str, FieldSpec]
    rules: list[dict[str, Any]]

    @property
    def pushed_fields(self) -> list[str]:
        return [n for n, f in self.fields.items() if not f.virtual]

    @property
    def required_fields(self) -> list[str]:
        return [n for n, f in self.fields.items() if f.required and not f.virtual]


def load_schema(path: str | Path) -> Schema:
    raw = yaml.safe_load(Path(path).read_text())
    fields = {}
    for name, spec in raw["fields"].items():
        fields[name] = FieldSpec(
            name=name, type=spec["type"], required=spec.get("required", False), unique=spec.get("unique", False),
            virtual=spec.get("virtual", False), values=spec.get("values", []), aliases=spec.get("aliases", []),
            description=spec.get("description", ""), min=spec.get("min"),
        )
    return Schema(entity=raw["entity"], identity_keys=raw.get("identity", {}).get("keys", []),
                  fields=fields, rules=raw.get("rules", []))
