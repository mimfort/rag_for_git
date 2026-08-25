"""Сборка единого контекста задачи для скилла solve-task (PRI-248).

Свёртка детерминированной части скилла: преflight (свежесть индекса, теплота
сводок, разрешённая доска) и сбор контекста (задача, связанные и похожие
задачи, подсистемы, релевантный код, тест-образцы) — за один вызов вместо
8-12 тул-раундов. За LLM остаётся relevance-фильтр и сборка брифа.

Модуль намеренно не знает про Settings и компоненты: источники секций
приходят объектом-провайдером, поэтому вся fail-open-таблица тестируется
без Postgres, Neo4j и сети.
"""
from __future__ import annotations

import logging

from reviewer.mcp.subqueries import build_subqueries
from reviewer.storage_health import (
    CAUSE_STORAGE_UNAVAILABLE, CAUSE_UNKNOWN, is_storage_unavailable, storage_remedy,
)

log = logging.getLogger(__name__)

SECTIONS = (
    "preflight", "task_board", "task", "related", "subsystems",
    "code", "test_exemplars", "gaps", "warnings",
)

STORAGE_REASON = "хранилище не отвечает"
SKIPPED_REASON = "пропущено: хранилище не отвечает"


def gap(section: str, reason: str, *, cause: str = CAUSE_UNKNOWN,
        remedy: str | None = None) -> dict:
    """Структурная запись о пробеле: секция, причина и её класс, без секретов.

    `cause` — машиночитаемый класс причины: скилл и тесты ветвятся по нему, а не
    по прозе `reason`. `remedy` — команда-лекарство, когда она есть и уместна.
    """
    return {"section": section, "reason": reason, "cause": cause, "remedy": remedy}


class _StorageState:
    """Взведён ли флаг «хранилище не отвечает» и какое лекарство называть.

    Флаг живёт на один вызов `build_task_context`: первая же недоступность
    хранилища отменяет остальные store-секции, иначе каждая добавила бы к
    времени ответа собственный таймаут пула (30 с × 8 секций).
    """

    def __init__(self, remedy: str | None) -> None:
        self.remedy = remedy
        self.down = False


def _safe(payload: dict, section: str, produce, default, reason: str,
          state: _StorageState):
    """Собрать секцию fail-open: сбой → default + запись в gaps.

    При взведённом `state.down` источник не вызывается вовсе — секция получает
    свой default и запись о пропуске, поэтому payload по-прежнему содержит все
    ключи `SECTIONS`.
    """
    if state.down:
        payload["gaps"].append(gap(section, SKIPPED_REASON,
                                   cause=CAUSE_STORAGE_UNAVAILABLE,
                                   remedy=state.remedy))
        return default
    try:
        return produce()
    except Exception as exc:  # noqa: BLE001 — источник секции недоступен, это штатный случай
        log.warning("prepare_task_context: секция %s недоступна", section, exc_info=True)
        if is_storage_unavailable(exc):
            state.down = True
            payload["gaps"].append(gap(section, STORAGE_REASON,
                                       cause=CAUSE_STORAGE_UNAVAILABLE,
                                       remedy=state.remedy))
        else:
            payload["gaps"].append(gap(section, reason))
        return default


def _query(task: dict | None, key: str) -> str:
    """Запрос ретрива: заголовок и начало описания задачи либо сам ключ.

    Board-less вход (свободный текст вместо ключа) остаётся рабочим: без
    задачи в сторе запросом становится сама формулировка пользователя.
    """
    if not task:
        return key
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    head = "\n".join(description.splitlines()[:8])
    return f"{title}. {head}".strip(". ").strip() or key


def _queries(task: dict | None, key: str) -> list[str]:
    """Набор подзапросов секции code: продакшн-запрос плюс структура задачи.

    Первый элемент — ровно _query(task, key), поэтому на задаче без списков
    набор вырождается в один запрос и ретрив ведёт себя как раньше.
    """
    return build_subqueries(task, _query(task, key))


def _test_queries(task: dict | None, key: str) -> list[str]:
    """Те же подзапросы, но про тесты области — префиксом "как тестируется: "."""
    return [f"как тестируется: {query}" for query in _queries(task, key)]


def _remedy(deps) -> str | None:
    """Лекарство по эндпоинтам хранилищ, если провайдер умеет их назвать.

    Читается через `getattr`, как `augment_gaps`: модуль намеренно не знает про
    Settings, а старый провайдер без этого метода обязан продолжать работать.
    """
    getter = getattr(deps, "storage_endpoints", None)
    if not callable(getter):
        return None
    try:
        return storage_remedy(*(getter() or ()))
    except Exception:  # noqa: BLE001 — источник эндпоинтов недоступен, это не повод падать
        log.warning("prepare_task_context: эндпоинты хранилищ недоступны", exc_info=True)
        return None


def build_task_context(deps, *, repo: str, key: str, branch: str,
                       warm_board: bool = True) -> dict:
    """Единый payload контекста задачи. Ни один сбой секции не прерывает сборку."""
    payload: dict = {section: None for section in SECTIONS}
    payload["gaps"] = []
    payload["warnings"] = []
    state = _StorageState(_remedy(deps))

    payload["preflight"] = _safe(
        payload, "preflight", lambda: deps.preflight(repo, branch), None,
        "статус индекса недоступен", state)
    board = _safe(
        payload, "task_board", lambda: deps.task_board(repo, branch), None,
        "конфиг доски не разрешён", state)
    payload["task_board"] = board
    project = (board or {}).get("project")

    if warm_board and board:
        result = _safe(
            payload, "warm_board", lambda: deps.warm_board(repo, branch), None,
            "прогрев доски не выполнен", state)
        if result is not None:
            payload["warnings"].append({"warm_board": result})
    elif warm_board and not board:
        payload["gaps"].append(gap("warm_board", "доска не настроена"))

    task = _safe(payload, "task", lambda: deps.task(key, project), None,
                 "задача не прочитана из стора", state)
    payload["task"] = task
    if task is None and not any(g["section"] == "task" for g in payload["gaps"]):
        payload["gaps"].append(gap("task", "задачи нет в сторе"))

    query = _query(task, key)
    payload["related"] = {
        "linked": _safe(payload, "related.linked",
                        lambda: deps.linked(key, project), "", "граф задач недоступен",
                        state),
        "similar": _safe(payload, "related.similar",
                         lambda: deps.similar(query, project), "", "корпус задач недоступен",
                         state),
    }
    payload["subsystems"] = _safe(
        payload, "subsystems", lambda: deps.subsystems(repo, branch, query), None,
        "сводки подсистем недоступны", state)
    payload["code"] = _safe(
        payload, "code", lambda: deps.code(repo, branch, _queries(task, key)), "",
        "поиск по коду недоступен", state)
    for reason in getattr(deps, "augment_gaps", []) or []:
        payload["gaps"].append(gap("code.augment", reason))
    payload["test_exemplars"] = _safe(
        payload, "test_exemplars",
        lambda: deps.test_exemplars(repo, branch, _test_queries(task, key)), "",
        "поиск по тестам недоступен", state)
    return payload
