"""Учёт токенов прогона с разбивкой по этапам (analyze/verify/synthesize)."""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class _StageCounters:
    """Счётчики для одного этапа."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0   # фактическая стоимость в USD (OpenRouter usage.cost)


class UsageLog:
    """Копит usage_metadata LLM-ответов с разбивкой по этапам (analyze/verify/synthesize).

    Потокобезопасен: analyze-узлы LangGraph выполняются параллельно.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, _StageCounters] = {}

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def add(self, stage: str, message) -> None:
        """Зафиксировать usage_metadata одного LLM-ответа.

        Args:
            stage:   название этапа (например, ``"analyze"``, ``"verify"``, ``"synthesize"``).
            message: LLM-ответ (langchain AIMessage или любой объект).
                     Ожидается атрибут ``usage_metadata`` — dict с ключами
                     ``input_tokens``, ``output_tokens`` и опционально
                     ``input_token_details["cache_read"]``. Фактическая стоимость
                     берётся из ``response_metadata["token_usage"]["cost"]`` (USD).
                     При отсутствии метаданных учитывается только счётчик вызовов.
                     Никогда не бросает исключений.
        """
        try:
            meta = getattr(message, "usage_metadata", None)
            rmeta = getattr(message, "response_metadata", None)
            with self._lock:
                if stage not in self._stages:
                    self._stages[stage] = _StageCounters()
                c = self._stages[stage]
                c.calls += 1
                if isinstance(meta, dict):
                    c.input_tokens += int(meta.get("input_tokens", 0))
                    c.output_tokens += int(meta.get("output_tokens", 0))
                    details = meta.get("input_token_details") or {}
                    c.cache_read_tokens += int(details.get("cache_read", 0))
                if isinstance(rmeta, dict):
                    token_usage = rmeta.get("token_usage") or {}
                    c.cost += float(token_usage.get("cost") or 0.0)
        except Exception:  # noqa: BLE001 — учёт не должен ломать агент
            pass

    def snapshot(self) -> dict[str, dict]:
        """Вернуть снимок счётчиков по этапам в виде словаря (потокобезопасно).

        Возвращает::

            {
                "analyze": {
                    "calls": 12,
                    "input_tokens": 45230,
                    "output_tokens": 3400,
                    "cache_read_tokens": 30100,
                    "cost": 0.0123,
                },
                ...
            }

        Изменения состояния после вызова на возвращённый dict не влияют.
        """
        with self._lock:
            return {
                stage: {
                    "calls": c.calls,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "cache_read_tokens": c.cache_read_tokens,
                    "cost": c.cost,
                }
                for stage, c in self._stages.items()
            }

    def report(self) -> str:
        """Вернуть многострочную сводку по этапам на русском языке.

        Возвращает пустую строку, если вызовов не было.
        Формат строки этапа::

            analyze: 12 вызовов, in 45230 (кэш 30100), out 3400, $0.0123

        Строка «итого» добавляется всегда при наличии хотя бы одного этапа.
        """
        with self._lock:
            snapshot = dict(self._stages)

        if not snapshot:
            return ""

        lines: list[str] = []
        total = _StageCounters()

        for stage, c in snapshot.items():
            lines.append(_format_stage(stage, c))
            total.calls += c.calls
            total.input_tokens += c.input_tokens
            total.output_tokens += c.output_tokens
            total.cache_read_tokens += c.cache_read_tokens
            total.cost += c.cost

        if len(snapshot) > 1:
            lines.append(_format_stage("итого", total))

        return "\n".join(lines)


# ------------------------------------------------------------------
# Вспомогательная функция
# ------------------------------------------------------------------

def _format_stage(label: str, c: _StageCounters) -> str:
    """Отформатировать одну строку сводки."""
    cache_part = f" (кэш {c.cache_read_tokens})" if c.cache_read_tokens else ""
    cost_part = f", ${c.cost:.4f}" if c.cost else ""
    return (f"{label}: {c.calls} вызовов, in {c.input_tokens}{cache_part}, "
            f"out {c.output_tokens}{cost_part}")
