"""Pure reviewer diagnostics shared by MCP tools and native hook wrappers."""

from __future__ import annotations

import hashlib
import json
import re

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
    "verify_task_brief": frozenset({"ok", "evidence_unavailable"}),
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


def _codex_tool_response(payload: dict, tool: str) -> object:
    """Extract the result from a Codex JSONL or App Server tool event."""
    if payload.get("type") == "item.completed":
        item = payload.get("item")
    elif payload.get("method") == "item/completed":
        params = payload.get("params")
        item = params.get("item") if isinstance(params, dict) else None
    elif payload.get("type") in ("mcp_tool_call", "mcpToolCall"):
        item = payload
    else:
        return payload
    if not isinstance(item, dict) or item.get("type") not in ("mcp_tool_call", "mcpToolCall"):
        return None
    server = item.get("server")
    if item.get("tool") != tool or f"mcp__{server}__" not in MCP_TOOL_PREFIXES:
        return None
    result = item.get("result")
    return result if result is not None else item.get("error")


def _unwrap(payload: dict | None) -> dict | None:
    """Read the structured status in standard MCP and exported Codex results."""
    if payload is None or "status" in payload:
        return payload
    for key in ("structuredContent", "structured_content", "result"):
        if (structured := _response_dict(payload.get(key))) is not None:
            return structured
    content = payload.get("content")
    if isinstance(content, list) and len(content) == 1:
        block = content[0]
        if isinstance(block, dict) and block.get("type") == "text":
            if (structured := _response_dict(block.get("text"))) is not None:
                return structured
    return payload


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
    event = _response_dict(response)
    rendered = _codex_tool_response(event, tool) if event is not None else response
    decoded = _response_dict(rendered)
    payload = _unwrap(decoded)
    if is_foreign(rendered) or (payload is not decoded and is_foreign(payload)):
        return None

    text = response_text(rendered)
    if _TRACEBACK.search(text) and _REVIEWER_FRAME.search(text):
        return "tool_exception", signature("tool_exception", tool, _frame_hint(text))

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


def diagnose(tool: str, response: str) -> dict:
    """Return only a classification, excluding raw exception data."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", tool):
        return {"status": "clear"}
    detected = detect(MCP_TOOL_PREFIXES[0] + tool, response)
    if detected is None:
        return {"status": "clear"}
    kind, identifier = detected
    return {"status": "suspected_defect", "kind": kind, "signature": identifier}
