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

Модуль stdlib-only и self-contained: хуки запускает системный python3, для которого
пакет ``reviewer`` не установлен. Классы симптомов синхронизированы с
``reviewer/bugreport/triage.py`` вручную и закреплены guard-тестом.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile

#: Оба варианта имён reviewer-тулов: прямой MCP-сервер и он же внутри плагина.
MCP_TOOL_PREFIXES = (
    "mcp__reviewer__",
    "mcp__plugin_rag-reviewer_reviewer__",
)

#: Категории BoardProviderError — все до единой чужие (reviewer/tasks/boards/errors.py).
FOREIGN_ERROR_CATEGORIES = frozenset({
    "configuration",
    "authentication",
    "permission",
    "not_found",
    "rate_limit",
    "transient",
    "unsupported",
})

#: Исходы, которые являются штатной работой, а не сбоем.
BENIGN_STATUSES = frozenset({
    "ok", "skipped", "preview", "disabled", "not_reported", "suppressed", "declined",
    "published", "commented", "fallback", "partial", "pending",
})

#: Текстовые признаки чужой проблемы. Это НАШИ собственные сообщения об ошибках и
#: устойчивые формы внешних сбоев, поэтому сопоставление детерминировано. Список
#: намеренно широкий: пропущенный дефект — потеря сигнала, лишнее срабатывание —
#: потеря доверия ко всему каналу.
FOREIGN_SIGNALS = (
    # Хранилища не подняты (docker compose up -d)
    "docker compose", "connection refused", "could not connect", "connect call failed",
    "could not translate host name", "no route to host", "connection reset",
    "psycopg", "operationalerror", "serviceunavailable", "servicunavailable",
    "neo4j", "postgres", "postgresql", "paradedb", "не подняты", "не поднят",
    "unable to retrieve routing information", "defunct connection",
    # Отсутствующие ключи и токены
    "voyage_api_key", "github_token", "gitlab_token", "api_key", "не задан",
    "not configured", "no credentials", "missing credential", "unauthorized",
    # Лимиты и внешние сервисы
    "rate limit", "rate_limit", "too many requests", "429", "quota", "throttl",
    "502", "503", "504", "bad gateway", "gateway timeout", "service unavailable",
    "timed out", "timeout", "read timeout", "connect timeout",
    "401", "403", "404", "forbidden", "not found", "permission denied",
    # Состояние индекса и веток — конфигурация, а не дефект
    "индекс не построен", "index not built", "no index", "устарел", "stale index",
    "не отслеживается", "not tracked", "review_branches", "branch not tracked",
    # Клиентское окружение. Только фразы: голое "mcp" встречается в наших же путях
    # (reviewer/mcp/service.py) и глушило бы ровно те трейсбеки, ради которых хук есть.
    "mcp не подключ", "mcp server is not", "mcp not connected", "сервер не подключ",
)

#: Документированные значения ``status`` по тулам. Ответ со статусом вне набора —
#: расхождение с собственным контрактом. Таблица намеренно узкая: неизвестный тул
#: не проверяется вовсе, потому что «я не знаю контракта» и «контракт нарушен» —
#: разные вещи, и вторая не должна выводиться из первой.
DOCUMENTED_STATUSES = {
    "finish_task": frozenset({"ok", "error"}),
    "create_task": frozenset({"ok", "error"}),
    "sync_board": frozenset({"ok", "error"}),
    "prepare_review": frozenset({"ok", "skipped", "error"}),
    "report_bug": frozenset({
        "disabled", "not_reported", "suppressed", "declined",
        "preview", "published", "commented", "fallback",
    }),
}

#: Фрейм traceback, ведущий в код самого reviewer. Проверяется именно путь фрейма,
#: а не наличие слова «reviewer» в тексте: сообщение об ошибке доски тоже его содержит.
_REVIEWER_FRAME = re.compile(
    r'File "[^"\n]*[/\\]reviewer[/\\][^"\n]*\.py"|File "reviewer[/\\][^"\n]*\.py"')
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")
_BUG_REPORTS_OFF = re.compile(r"^\s*bug_reports\s*:\s*(false|no|off)\s*$",
                              re.IGNORECASE | re.MULTILINE)

_NUDGE = (
    "Замечен возможный дефект самого reviewer ({kind}, тул {tool}). Это не проблема "
    "окружения и не ошибка внешнего сервиса — такие случаи хук пропускает молча.\n"
    "Скажи об этом пользователю одной строкой и предложи скилл report-bug. Публикация "
    "issue происходит только после его явного согласия; ты ничего не публикуешь сам.\n"
    "Если разберёшься и увидишь, что дефекта нет, просто продолжай работу."
)


def logical_tool(tool_name: str) -> str:
    """Имя тула без MCP-префикса; чужой тул → пустая строка."""
    for prefix in MCP_TOOL_PREFIXES:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix):]
    return ""


def response_text(response: object) -> str:
    """Плоский текст ответа тула для поиска признаков (структура не важна).

    Собирается обходом строковых значений, а НЕ через ``json.dumps``: дамп экранирует
    кавычки, и фрейм ``File "…/reviewer/mcp/service.py"`` превращался в ``File \\"…``,
    мимо которого проходила регулярка фреймов — то есть детектор молчал ровно на своём
    главном сценарии.
    """
    parts: list[str] = []
    _collect_strings(response, parts)
    return "\n".join(parts)


def _collect_strings(value: object, out: list[str], depth: int = 0) -> None:
    if depth > 8:      # предохранитель от самоссылающихся структур
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            _collect_strings(item, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_strings(item, out, depth + 1)
    elif value is not None:
        out.append(str(value))


def _response_dict(response: object) -> dict | None:
    """Ответ как dict: FastMCP отдаёт и готовый объект, и JSON-строку."""
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def is_foreign(response: object) -> bool:
    """Чужая проблема? Проверяется ПЕРВОЙ и перекрывает любые признаки дефекта."""
    payload = _response_dict(response)
    if payload is not None:
        if str(payload.get("category") or "") in FOREIGN_ERROR_CATEGORIES:
            return True
        if payload.get("retryable") is True:
            return True
    text = response_text(response).lower()
    return any(signal in text for signal in FOREIGN_SIGNALS)


def detect(tool_name: str, response: object) -> tuple[str, str] | None:
    """Вернуть (kind, signature) для дефекта инструмента или None, если его нет.

    Детектируются только два класса, каждый из которых распознаётся по форме:
    ``tool_exception`` — фрейм reviewer/* в traceback; ``contract_violation`` —
    значение ``status`` вне документированного набора тула. Нарушения инвариантов
    (идемпотентность, дедуп, счётчики) остаются за моделью: их нельзя увидеть в
    одном ответе, а гадать по одному вызову — прямой путь к ложным срабатываниям.
    """
    tool = logical_tool(tool_name)
    if not tool:
        return None
    if is_foreign(response):
        return None

    text = response_text(response)
    if _TRACEBACK.search(text) and _REVIEWER_FRAME.search(text):
        return "tool_exception", signature("tool_exception", tool, _frame_hint(text))

    payload = _response_dict(response)
    if payload is not None and tool in DOCUMENTED_STATUSES:
        status = payload.get("status")
        if isinstance(status, str) and status not in DOCUMENTED_STATUSES[tool]:
            return "contract_violation", signature("contract_violation", tool, status)
    return None


def _frame_hint(text: str) -> str:
    """Последний фрейм reviewer/* — стабильная часть сигнатуры одного и того же сбоя."""
    frames = _REVIEWER_FRAME.findall(text)
    return frames[-1] if frames else ""


def signature(kind: str, tool: str, detail: str = "") -> str:
    """Сигнатура симптома: та же роль, что у triage.signature на сервере."""
    raw = "|".join((kind, tool, detail))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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
