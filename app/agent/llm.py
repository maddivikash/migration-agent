"""Optional open-source LLM (Ollama / any OpenAI-compatible server).

The agent is deterministic-first. The LLM is consulted ONLY for genuinely
ambiguous judgement calls (column tie-breaks, unknown enum values) and its
answer is treated as a *suggestion with a confidence*, never as truth: a
high-confidence agreement with the heuristic lets the agent proceed; anything
else still goes to the human, with the LLM's opinion attached as context.
If no server is reachable the agent degrades gracefully to heuristics-only.
"""
from __future__ import annotations
import json, os, re
import httpx

BASE_URL = os.getenv("MIGRATION_LLM_BASE_URL", "http://localhost:11434/v1")
MODEL = os.getenv("MIGRATION_LLM_MODEL", "qwen2.5:7b")
MODE = os.getenv("MIGRATION_LLM", "auto")   # auto | off
API_KEY = os.getenv("MIGRATION_LLM_API_KEY", "ollama")


class LLM:
    def __init__(self):
        self.available = False
        self.model = MODEL
        self.detail = "disabled" if MODE == "off" else "not checked"

    def probe(self) -> bool:
        if MODE == "off":
            self.available = False; self.detail = "disabled via MIGRATION_LLM=off"; return False
        try:
            r = httpx.get(f"{BASE_URL}/models", timeout=2.0, headers={"Authorization": f"Bearer {API_KEY}"})
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", [])]
            if ids and self.model not in ids:
                self.model = ids[0]
            self.available = True
            self.detail = f"connected to {BASE_URL} ({self.model})"
        except Exception as e:  # noqa: BLE001
            self.available = False
            self.detail = f"no LLM reachable at {BASE_URL} - heuristics only ({type(e).__name__})"
        return self.available

    def _ask_json(self, system: str, user: str) -> dict | None:
        if not self.available:
            return None
        try:
            r = httpx.post(f"{BASE_URL}/chat/completions", timeout=30.0,
                           headers={"Authorization": f"Bearer {API_KEY}"},
                           json={"model": self.model, "temperature": 0,
                                 "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0)) if m else None
        except Exception:  # noqa: BLE001
            return None

    def tie_break_column(self, column: str, samples: list[str], candidates: list[str], descriptions: dict[str, str]) -> dict | None:
        """-> {"field": str|None, "confidence": 0-1, "reason": str} or None"""
        sys = ("You map legacy HR export columns to a target schema. Answer ONLY with JSON: "
               '{"field": <one of the candidates or null>, "confidence": <0..1>, "reason": <short>}. '
               "Use null if none of the candidates is clearly right.")
        user = (f"Source column: {column!r}\nSample values: {samples[:6]}\nCandidate target fields:\n" +
                "\n".join(f"- {c}: {descriptions.get(c,'')}" for c in candidates))
        out = self._ask_json(sys, user)
        if out and (out.get("field") in candidates or out.get("field") is None):
            return {"field": out.get("field"), "confidence": float(out.get("confidence", 0) or 0), "reason": out.get("reason", "")}
        return None

    def suggest_enum(self, field: str, value: str, allowed: list[str], context: dict) -> dict | None:
        sys = ("You normalise messy HR data values. Answer ONLY with JSON: "
               '{"value": <one of allowed or null>, "confidence": <0..1>, "reason": <short>}.')
        user = f"Field: {field}\nRaw value: {value!r}\nAllowed values: {allowed}\nOther fields of the same record: {context}"
        out = self._ask_json(sys, user)
        if out and (out.get("value") in allowed or out.get("value") is None):
            return {"value": out.get("value"), "confidence": float(out.get("confidence", 0) or 0), "reason": out.get("reason", "")}
        return None
