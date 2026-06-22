"""Pydantic-схемы LLM-facing вывода ревью — единый источник (PRI-156).

``FindingIn``/``VerdictIn`` — канон схемы findings/verdicts: из них FastMCP
выводит схему тулов ``submit_findings``/``submit_verdicts`` (энфорс на тул-границе),
из них же строится внутренний ``Finding`` (``Finding.from_in``). Валидаторы —
дословный порт ``_finding_from_dict`` (мягкая коэрция, сохраняет PRI-144):
кривые значения коэрцируются с дефолтами, обязателен только ``file``.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})


def _coerce_int(value) -> int | None:
    """int-коэрция LLM-значения: int("42") → 42, None/мусор → None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FixIn(BaseModel):
    """Applyable-правка: точная замена диапазона строк НОВОЙ версии (RIGHT)."""

    model_config = ConfigDict(extra="ignore")

    start_line: int | None = None
    end_line: int | None = None
    replacement: str | None = None


class FindingIn(BaseModel):
    """LLM-facing находка. Мягкая коэрция = порт ``_finding_from_dict``."""

    model_config = ConfigDict(extra="ignore")

    file: str
    category: str = "correctness"
    severity: str = "medium"
    side: str = "RIGHT"
    line: int | None = None
    code_quote: str | None = None
    message: str = ""
    suggestion: str | None = None
    confidence: float = 0.1
    fix: FixIn | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        return str(v) if v else "correctness"

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, v):
        return v if isinstance(v, str) and v in _VALID_SEVERITIES else "medium"

    @field_validator("side", mode="before")
    @classmethod
    def _side(cls, v):
        return v if v in ("RIGHT", "LEFT") else "RIGHT"

    @field_validator("line", mode="before")
    @classmethod
    def _line(cls, v):
        return _coerce_int(v)

    @field_validator("code_quote", mode="before")
    @classmethod
    def _code_quote(cls, v):
        return v if isinstance(v, str) else None

    @field_validator("message", mode="before")
    @classmethod
    def _message(cls, v):
        return str(v) if v else ""

    @field_validator("suggestion", mode="before")
    @classmethod
    def _suggestion(cls, v):
        return v if isinstance(v, str) else None

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v):
        try:
            c = float(v)
        except (TypeError, ValueError):
            c = 0.1   # не оценено = спекулятивно (ниже честного потолка 0.4) → отсекается гейтом
        return max(0.0, min(1.0, c))

    @field_validator("fix", mode="before")
    @classmethod
    def _fix(cls, v):
        # Кривая/неполная fix отбрасывается целиком (как в _finding_from_dict).
        if not isinstance(v, dict):
            return None
        start = _coerce_int(v.get("start_line"))
        end = _coerce_int(v.get("end_line"))
        replacement = v.get("replacement")
        if start is None or end is None or not isinstance(replacement, str):
            return None
        return {"start_line": start, "end_line": end, "replacement": replacement}


class VerdictIn(BaseModel):
    """Вердикт verify по находке с server-assigned id."""

    model_config = ConfigDict(extra="ignore")

    id: str
    is_real: bool
