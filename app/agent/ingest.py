"""Read CSV / Excel exports into raw string tables, keeping provenance (file, row)."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import pandas as pd


@dataclass
class SourceTable:
    name: str            # file name
    columns: list[str]
    rows: list[dict]     # every value is str ("" for blank); plus "_row" = 1-based source row


def read_source(path: str | Path) -> SourceTable:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    elif path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    df = df.fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    rows = []
    for i, rec in enumerate(df.to_dict(orient="records")):
        clean = {k: ("" if v is None else str(v).strip()) for k, v in rec.items()}
        clean["_row"] = i + 2  # header is row 1
        rows.append(clean)
    return SourceTable(name=path.name, columns=list(df.columns), rows=rows)


def profile_column(rows: list[dict], col: str, n: int = 8) -> dict:
    """Cheap statistics the mapper uses to reason about a column's content."""
    vals = [r[col] for r in rows if r.get(col, "") != ""]
    return {
        "non_empty": len(vals), "total": len(rows),
        "distinct": len(set(vals)),
        "samples": list(dict.fromkeys(vals))[:n],
    }
