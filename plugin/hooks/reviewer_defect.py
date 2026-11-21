"""PostToolUse-хук: детерминированно замечает дефект самого reviewer (PRI-240).

Канал репорта багов (PRI-239) держался на внимательности модели: заметить дефект
предлагали description скилла и общий блок промпта. Если модель не обратила внимания
или молча обошла проблему, сигнал терялся — ровно тот сценарий, который канал и
закрывал. Этот хук убирает «заметить» из зоны решений модели; решением остаётся
только апрув публикации, каким он и должен быть.

Главный риск — ложные срабатывания: шумящий хук выключат целиком, и канал умрёт
вместе с ним. Поэтому allowlist чужих сигналов проверяется ПЕРВЫМ и перекрывает
любые признаки дефекта: не поднятый Postgres, отсутствующий ключ Voyage, лимит
доски и 403 никогда не порождают напоминания.

Хук запускается системным Python. Детектор поставляется рядом как проекция
reviewer/bugreport/diagnostics.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile

from _reviewer_diagnostics import (
    DOCUMENTED_STATUSES as DOCUMENTED_STATUSES,
    FOREIGN_ERROR_CATEGORIES as FOREIGN_ERROR_CATEGORIES,
    MCP_TOOL_PREFIXES as MCP_TOOL_PREFIXES,
    detect,
    logical_tool,
)

_BUG_REPORTS_OFF = re.compile(r"^\s*bug_reports\s*:\s*(false|no|off)\s*$",
                              re.IGNORECASE | re.MULTILINE)

_NUDGE = (
    "Замечен возможный дефект самого reviewer ({kind}, тул {tool}). Это не проблема "
    "окружения и не ошибка внешнего сервиса — такие случаи хук пропускает молча.\n"
    "Скажи об этом пользователю одной строкой и предложи скилл report-bug. Публикация "
    "issue происходит только после его явного согласия; ты ничего не публикуешь сам.\n"
    "Если разберёшься и увидишь, что дефекта нет, просто продолжай работу."
)


def channel_enabled(cwd: str) -> bool:
    """Уважать оба выключателя канала: деплойный env и per-repo `.review.yml`.

    YAML читается регуляркой намеренно: хук обязан оставаться stdlib-only, а тащить
    парсер ради одного булева ключа — цена выше пользы. Нераспознанный файл считается
    «канал включён»: это дефолт политики, и хук не должен молчать из-за своей же
    неспособности разобрать конфиг.
    """
    if str(os.environ.get("REVIEW_BUG_REPORTS", "")).strip().lower() in {"false", "0", "no"}:
        return False
    try:
        with open(os.path.join(cwd or ".", ".review.yml"), encoding="utf-8") as handle:
            return not _BUG_REPORTS_OFF.search(handle.read())
    except OSError:
        return True


def _state_path(session_id: str) -> str:
    digest = hashlib.sha256((session_id or "default").encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"reviewer-defect-{digest}.json")


def already_seen(session_id: str, value: str) -> bool:
    """Один и тот же симптом напоминается один раз за сессию.

    Состояние в tempdir, а не в памяти: каждый вызов хука — отдельный процесс,
    поэтому иначе напоминание повторялось бы на каждом вызове тула.
    """
    path = _state_path(session_id)
    seen: list = []
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, list):
            seen = [item for item in loaded if isinstance(item, str)]
    except (OSError, ValueError):
        seen = []
    if value in seen:
        return True
    seen.append(value)
    try:
        directory = os.path.dirname(path) or "."
        fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(seen[-200:], handle)
        os.replace(temp_path, path)
    except OSError:
        pass      # дедуп — удобство, а не корректность: сбой записи не должен глушить хук
    return False


def emit(kind: str, tool: str) -> None:
    """Подмешать напоминание в контекст модели.

    В нём только форма сбоя — класс и имя тула. Ни текста ошибки, ни путей, ни
    фрагментов кода: собрать и анонимизировать отчёт — работа server-side report_bug,
    и дублировать её в хуке значило бы завести второй, непроверенный санитайзер.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": _NUDGE.format(kind=kind, tool=tool),
        }
    }, ensure_ascii=False))


def run(payload: dict) -> int:
    """Оркестрация fail-open: любая внутренняя проблема — тихий выход."""
    try:
        tool_name = str(payload.get("tool_name") or "")
        tool = logical_tool(tool_name)
        if not tool:
            return 0
        if not channel_enabled(str(payload.get("cwd") or "")):
            return 0
        detected = detect(tool_name, payload.get("tool_response"))
        if detected is None:
            return 0
        kind, value = detected
        if already_seen(str(payload.get("session_id") or ""), value):
            return 0
        emit(kind, tool)
    except Exception:
        return 0
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    return run(payload)


if __name__ == "__main__":
    sys.exit(main())
