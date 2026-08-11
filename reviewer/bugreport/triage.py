"""Триаж репорта: наш баг или чужая проблема, насколько серьёзно, один ли он (PRI-239).

Канал обязан молчать на чужих проблемах, иначе он превратится в шум и его выключат.
Поэтому решение принимается детерминированно на Python, а не «на усмотрение модели»:
клиент присылает форму сбоя (класс симптома, категорию причины, признаки), сервер
выносит вердикт. Классификация ведётся по форме, а не по тексту — как в
``reviewer/config/fetch_errors.py``: текст исключения содержит URL и токены.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

Severity = Literal["blocker", "degraded", "contract"]

#: Классы симптомов, которые ЯВЛЯЮТСЯ дефектом инструмента.
OUR_KINDS: dict[str, str] = {
    "tool_exception": "исключение с фреймами reviewer/* в трейсбеке",
    "contract_violation": "ответ MCP-тула не соответствует своему докстринг-контракту",
    "skill_impossible": "скилл предписывает шаг, невыполнимый доступным набором тулов",
    "skill_contradiction": "инструкция скилла противоречит серверному контракту",
    "invariant_violation": "нарушен заявленный инвариант (идемпотентность, дедуп, GC, счётчики)",
    "deterministic_repro": "сбой детерминирован и воспроизводится на минимальном примере",
}

#: Классы симптомов, которые НЕ являются дефектом инструмента.
FOREIGN_KINDS: dict[str, str] = {
    "environment": "окружение и конфигурация (Postgres/Neo4j, ключи, MCP, индекс, ветка)",
    "external_service": "внешний сервис (429/5xx доски, лимит Voyage, GitHub/GitLab API, таймаут)",
    "user_code": "код пользователя (его тесты, конфликты git, его сборка)",
    "permission": "права и политика (403 доски, нет доступа к репо, запрет .review.yml)",
    "llm_behaviour": "поведение LLM-оркестратора (не вызвал тул, выдумал значение)",
}

#: Единственное исключение из FOREIGN_KINDS: причина поведения модели — в самой
#: инструкции скилла. Баг промпта — тоже наш баг (случай PRI-237).
_LLM_ESCAPE_HATCH = "skill_contradiction"

_SEVERITIES: tuple[Severity, ...] = ("blocker", "degraded", "contract")
_WORD = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class TriageVerdict:
    """Результат триажа: репортим ли, почему и насколько срочно."""

    ours: bool
    kind: str
    severity: Severity
    reason: str
    signature: str
    #: contract/cosmetic копится до конца сессии, blocker/degraded предлагается сразу.
    defer: bool

    def as_dict(self) -> dict:
        return {
            "ours": self.ours,
            "kind": self.kind,
            "severity": self.severity,
            "reason": self.reason,
            "signature": self.signature,
            "defer": self.defer,
        }


def normalize_severity(value: object, default: Severity = "degraded") -> Severity:
    text = str(value or "").strip().lower()
    if text in _SEVERITIES:
        return text   # type: ignore[return-value]
    if text in {"cosmetic", "minor", "doc", "docs"}:
        return "contract"
    if text in {"critical", "fatal", "blocking"}:
        return "blocker"
    return default


def signature(kind: str, tool: str = "", step: str = "", error_class: str = "") -> str:
    """Стабильная сигнатура симптома для дедупа предложений внутри сессии.

    Считается по форме (класс симптома + тул + шаг скилла + класс ошибки), а не по
    тексту: один и тот же дефект приходит с разными формулировками модели и без
    нормализации предлагался бы заново на каждом повторе.
    """
    parts = [_normalize_token(kind), _normalize_token(tool),
             _normalize_token(step), _normalize_token(error_class)]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def _normalize_token(value: object) -> str:
    return "-".join(_WORD.findall(str(value or "").lower()))


def triage(
    kind: str,
    *,
    severity: object = None,
    tool: str = "",
    step: str = "",
    error_class: str = "",
    caused_by_skill_instruction: bool = False,
) -> TriageVerdict:
    """Вынести вердикт по форме сбоя.

    ``caused_by_skill_instruction`` — единственная лазейка для ``llm_behaviour``:
    если модель повела себя неверно из-за противоречивой инструкции скилла, дефект
    наш (баг промпта), и класс симптома переписывается на ``skill_contradiction``.
    """
    normalized_kind = _normalize_token(kind).replace("-", "_")
    resolved_severity = normalize_severity(severity)

    if normalized_kind == "llm_behaviour" and caused_by_skill_instruction:
        normalized_kind = _LLM_ESCAPE_HATCH

    if normalized_kind in FOREIGN_KINDS:
        return TriageVerdict(
            ours=False,
            kind=normalized_kind,
            severity=resolved_severity,
            reason=f"не дефект инструмента: {FOREIGN_KINDS[normalized_kind]}",
            signature=signature(normalized_kind, tool, step, error_class),
            defer=False,
        )
    if normalized_kind not in OUR_KINDS:
        # Неизвестный класс не репортим: канал молчит там, где не уверен.
        return TriageVerdict(
            ours=False,
            kind=normalized_kind or "unknown",
            severity=resolved_severity,
            reason=("неизвестный класс симптома; репортим только перечисленные классы "
                    f"({', '.join(sorted(OUR_KINDS))})"),
            signature=signature(normalized_kind, tool, step, error_class),
            defer=False,
        )
    return TriageVerdict(
        ours=True,
        kind=normalized_kind,
        severity=resolved_severity,
        reason=OUR_KINDS[normalized_kind],
        signature=signature(normalized_kind, tool, step, error_class),
        defer=resolved_severity == "contract",
    )
