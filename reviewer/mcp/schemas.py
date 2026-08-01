"""Pydantic-схемы LLM-facing вывода ревью — единый источник (PRI-156).

``FindingIn``/``VerdictIn`` — канон схемы findings/verdicts: из них FastMCP
выводит схему тулов ``submit_findings``/``submit_verdicts`` (энфорс на тул-границе),
из них же строится внутренний ``Finding`` (``Finding.from_in``). Валидаторы —
дословный порт ``_finding_from_dict`` (мягкая коэрция, сохраняет PRI-144):
кривые значения коэрцируются с дефолтами, обязателен только ``file``.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from reviewer.tasks.subtasks import MAX_SUBTASKS

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


class SummaryFragmentIn(BaseModel):
    """LLM-facing пофайловый фрагмент сводки подсистемы."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    provenance: dict[str, JsonValue] = Field(default_factory=dict)


class SubtaskIn(BaseModel):
    """Строгий LLM-facing черновик нативной дочерней задачи."""

    model_config = ConfigDict(extra="forbid")

    title: str
    problem: str
    steps: list[str]
    criteria: list[str]
    context: str | None = None

    @field_validator("title", "problem", mode="before")
    @classmethod
    def _required_text(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("значение должно быть непустой строкой")
        return value.strip()

    @field_validator("steps", "criteria", mode="before")
    @classmethod
    def _required_items(cls, value: object) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("значение должно быть списком строк")
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("список должен содержать непустой элемент")
        return cleaned

    @field_validator("context", mode="before")
    @classmethod
    def _context(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("context должен быть строкой или null")  # noqa: TRY004
        return value.strip() or None


SubtasksIn = Annotated[
    list[SubtaskIn],
    Field(min_length=1, max_length=MAX_SUBTASKS),
]


class FindingIn(BaseModel):
    """LLM-facing находка. Мягкая коэрция = порт ``_finding_from_dict``."""

    model_config = ConfigDict(extra="ignore")

    file: str = Field(min_length=1)
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
    # PRI: причина reject (при is_real=false) для наблюдаемости; None = не указана.
    reason: str | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v):
        return v if isinstance(v, str) and v.strip() else None
