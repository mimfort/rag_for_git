"""Пошаговый трейс прогона агента (промпты, LLM-вызовы, tool-вызовы).

По образцу UsageLog: потокобезопасен (lock + общий счётчик seq),
никогда не бросает исключений — трейс не должен ломать агент.
"""
from __future__ import annotations

import threading

# Кап длины текстового поля шага (~8 КБ).
_TEXT_CAP = 8192
_TEXT_SUFFIX = "…(обрезано)"


def _cap_text(text: str) -> str:
    """Обрезать текст до _TEXT_CAP символов с пометкой при обрезке."""
    if len(text) <= _TEXT_CAP:
        return text
    return text[:_TEXT_CAP] + _TEXT_SUFFIX


def _extract_tokens_cost(message) -> tuple[int, float]:
    """Извлечь суммарные токены и стоимость из AIMessage (как в usage.py).

    Возвращает ``(tokens, cost)``, где tokens = input + output токены.
    При отсутствии метаданных — (0, 0.0). Никогда не бросает.
    """
    try:
        meta = getattr(message, "usage_metadata", None)
        rmeta = getattr(message, "response_metadata", None)
        tokens = 0
        cost = 0.0
        if isinstance(meta, dict):
            tokens = int(meta.get("input_tokens", 0)) + int(meta.get("output_tokens", 0))
        if isinstance(rmeta, dict):
            token_usage = rmeta.get("token_usage") or {}
            cost = float(token_usage.get("cost") or 0.0)
        return tokens, cost
    except Exception:  # noqa: BLE001 — никогда не бросаем
        return 0, 0.0


class TraceLog:
    """Копит пошаговый трейс прогона агента (промпты, LLM-вызовы, tool-вызовы).

    Потокобезопасен: analyze-узлы LangGraph выполняются параллельно → нужен
    общий lock и монотонно растущий счётчик ``seq``.

    Никогда не бросает исключений — обёртки ``try/except`` на каждом методе.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._steps: list[dict] = []

    # ------------------------------------------------------------------
    # Внутренние хелперы
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        """Атомарно выдать следующий порядковый номер (под локом)."""
        self._seq += 1
        return self._seq

    def _append(self, step: dict) -> None:
        """Добавить шаг в список (под локом)."""
        with self._lock:
            step["seq"] = self._next_seq()
            self._steps.append(step)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def record_prompt(self, stage: str, unit: str, text: str) -> None:
        """Зафиксировать стартовый промпт юнита (один раз на файл/юнит).

        Args:
            stage: название этапа (``"analyze"``, ``"verify"``, ``"synthesize"``).
            unit:  идентификатор юнита (путь файла, ``"(синтез)"`` и т.п.).
            text:  текст промпта (обрезается до ~8 КБ).
        """
        try:
            self._append({
                "stage": stage,
                "unit": unit,
                "kind": "prompt",
                "name": None,
                "text": _cap_text(str(text)),
                "tool_calls": None,
                "tokens": 0,
                "cost": 0.0,
            })
        except Exception:  # noqa: BLE001
            pass

    def record_llm_call(self, stage: str, unit: str, message) -> None:
        """Зафиксировать ответ LLM (AIMessage).

        Извлекает текст-рассуждение, список выбранных tool_calls (имя + args),
        количество токенов и стоимость.

        Args:
            stage:   название этапа.
            unit:    идентификатор юнита.
            message: объект AIMessage (или совместимый SimpleNamespace для тестов).
        """
        try:
            # Текст ответа
            content = getattr(message, "content", "")
            if isinstance(content, list):
                text = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            else:
                text = str(content) if content else ""

            # tool_calls — список {"name": str, "args": dict}
            raw_tool_calls = getattr(message, "tool_calls", None) or []
            tool_calls = [
                {"name": tc.get("name", ""), "args": tc.get("args", {})}
                for tc in raw_tool_calls
                if isinstance(tc, dict)
            ]

            tokens, cost = _extract_tokens_cost(message)

            self._append({
                "stage": stage,
                "unit": unit,
                "kind": "llm_call",
                "name": None,
                "text": _cap_text(text),
                "tool_calls": tool_calls if tool_calls else None,
                "tokens": tokens,
                "cost": cost,
            })
        except Exception:  # noqa: BLE001
            pass

    def record_tool_call(
        self,
        stage: str,
        unit: str,
        name: str,
        args: dict,
        result,
    ) -> None:
        """Зафиксировать вызов инструмента и его результат.

        Args:
            stage:  название этапа.
            unit:   идентификатор юнита.
            name:   имя инструмента (``"search_code"``, ``"get_related_symbols"`` и т.п.).
            args:   аргументы вызова.
            result: результат (преобразуется через ``str()``; обрезается до ~8 КБ).
        """
        try:
            self._append({
                "stage": stage,
                "unit": unit,
                "kind": "tool_call",
                "name": name,
                "text": _cap_text(str(result)),
                "tool_calls": [{"name": name, "args": args}],
                "tokens": 0,
                "cost": 0.0,
            })
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> list[dict]:
        """Вернуть упорядоченный по ``seq`` список шагов (потокобезопасно).

        Каждый шаг::

            {
                "seq":        int,               # монотонно растущий номер
                "stage":      str,               # analyze | verify | synthesize
                "unit":       str,               # путь файла или "(синтез)"
                "kind":       str,               # prompt | llm_call | tool_call
                "name":       str | None,        # имя тула (для tool_call/llm_call)
                "text":       str,               # обрезанный текст ответа/результата
                "tool_calls": list | None,       # [{name, args}] или None
                "tokens":     int,               # суммарные токены вызова (0 для tool_call)
                "cost":       float,             # стоимость вызова в USD (0.0 для tool_call)
            }

        Изменение возвращённого списка не влияет на внутреннее состояние.
        """
        with self._lock:
            return sorted([dict(s) for s in self._steps], key=lambda s: s["seq"])
