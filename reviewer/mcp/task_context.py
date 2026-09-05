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
    BACKEND_GRAPH, BACKEND_POSTGRES, CAUSE_EMBEDDER_UNAVAILABLE,
    CAUSE_STORAGE_UNAVAILABLE, CAUSE_UNKNOWN, DETAIL_AUTH_FAILED,
    DETAIL_MISSING_DATABASE, DETAIL_POOL_EXHAUSTED, SOURCE_EMBEDDER,
    StorageDiagnosis, classify_storage_failure, is_embedder_unavailable,
    is_storage_unavailable, storage_backend,
)

log = logging.getLogger(__name__)

SECTIONS = (
    "preflight", "task_board", "task", "related", "subsystems",
    "code", "test_exemplars", "gaps", "warnings",
)

STORAGE_REASON = "хранилище не отвечает"
SKIPPED_REASON = f"пропущено: {STORAGE_REASON}"

EMBEDDER_REASON = "эмбеддер не отвечает"
EMBEDDER_SKIPPED_REASON = f"пропущено: {EMBEDDER_REASON}"

# Базовая формулировка по источнику: у эмбеддера своя, потому что «хранилище
# не отвечает» про него — неправда, а именно эта неправда и была дефектом.
_SOURCE_REASONS = {
    SOURCE_EMBEDDER: (EMBEDDER_REASON, EMBEDDER_SKIPPED_REASON),
}
_DEFAULT_REASONS = (STORAGE_REASON, SKIPPED_REASON)

# Секции, которым нужен эмбеддер. Разметка явная, а не выведенная: preflight,
# task_board, warm_board, task и related.linked его не трогают вовсе, и замыкать
# их отказом Voyage значило бы терять данные, которые собрались бы без него.
_EMBEDDER_SECTIONS = frozenset({
    "related.similar", "subsystems", "code", "test_exemplars",
})

# Формулировки распознанных классов. Живут здесь, а не в storage_health: их
# читает LLM и вставляет в бриф, а у CLI свой адресат и свои строки.
DETAIL_REASONS = {
    DETAIL_AUTH_FAILED: "хранилище отвергло учётные данные",
    DETAIL_MISSING_DATABASE: "базы данных не существует",
    DETAIL_POOL_EXHAUSTED: "свободных соединений в пуле не осталось",
}


def gap(section: str, reason: str, *, cause: str = CAUSE_UNKNOWN,
        cause_detail: str | None = None, remedy: str | None = None) -> dict:
    """Структурная запись о пробеле: секция, причина и её класс, без секретов.

    `cause` — машиночитаемый класс причины: скилл и тесты ветвятся по нему, а не
    по прозе `reason`. `cause_detail` — уточнение внутри класса, когда оно
    установлено (PRI-277). `remedy` — команда-лекарство, когда она есть и уместна.
    """
    return {"section": section, "reason": reason, "cause": cause,
            "cause_detail": cause_detail, "remedy": remedy}


class _StorageState:
    """Какие хранилища не отвечают и каков вердикт по первому сбою каждого.

    Флаги живут на один вызов `build_task_context`: первая же недоступность
    хранилища отменяет остальные секции ЭТОГО хранилища, иначе каждая добавила
    бы к времени ответа собственный таймаут пула (30 с × 8 секций).

    Множество, а не один флаг (PRI-276): у графа и Postgres разные секции, и
    отказ Neo4j не должен отменять поиск по коду. Вердикт хранится по бэкенду —
    у неверного пароля Postgres и остановленного Neo4j причины разные, и один
    вердикт на двоих приписал бы одному чужое лекарство.

    Вердикт считается лениво, при первом реальном сбое: до PRI-277 лекарство
    фиксировалось на старте, когда исключения ещё нет, и потому не могло
    зависеть от причины.

    Источник, а не только хранилище (PRI-272): эмбеддер отказывает независимо
    от Postgres и Neo4j, и его отказ обязан отменять ровно те секции, которым
    он нужен. Класс причины хранится рядом с вердиктом — «лежит хранилище» и
    «не отвечает эмбеддер» это разные классы, а не разные детали одного.
    """

    def __init__(self, endpoints: tuple[str, ...]) -> None:
        self.endpoints = endpoints
        self.down: set[str] = set()
        self.diagnoses: dict[str, StorageDiagnosis] = {}
        self.causes: dict[str, str] = {}

    def mark(self, source: str, exc: BaseException | None = None) -> StorageDiagnosis:
        """Взвести флаг источника; вердикт считается по его первому сбою.

        `exc` необязателен: недоступность эмбеддера может прийти не броском, а
        структурным полем свода синка (секция `task`), и тогда классифицировать
        нечего — класс известен из самого источника.
        """
        self.down.add(source)
        if source not in self.diagnoses:
            if source == SOURCE_EMBEDDER:
                self.diagnoses[source] = StorageDiagnosis(None, None, None)
                self.causes[source] = CAUSE_EMBEDDER_UNAVAILABLE
            else:
                self.diagnoses[source] = (
                    classify_storage_failure(exc, *self.endpoints)
                    if exc is not None else StorageDiagnosis(None, None, None))
                self.causes[source] = CAUSE_STORAGE_UNAVAILABLE
        return self.diagnoses[source]

    def cause_of(self, source: str) -> str:
        """Класс причины по источнику; `unknown`, пока источник не помечен."""
        return self.causes.get(source, CAUSE_UNKNOWN)

    def is_down(self, source: str) -> bool:
        return source in self.down


def _source_gap(payload: dict, section: str, state: _StorageState, source: str,
                *, skipped: bool) -> None:
    """Записать в gaps пробел, вызванный недоступностью источника.

    Общая точка для трёх мест, различающихся только тем, пропущена секция или
    реально упала: skip- и except-ветки `_safe`, а также ветка
    `elif warm_board and not board` в build_task_context. Вердикт, класс и
    базовая формулировка берутся по источнику секции, а не общие на вызов.
    """
    diagnosis = state.diagnoses.get(source)
    base, skipped_base = _SOURCE_REASONS.get(source, _DEFAULT_REASONS)
    reason = skipped_base if skipped else base
    payload["gaps"].append(gap(
        section, _reason_with_detail(reason, diagnosis),
        cause=state.cause_of(source),
        cause_detail=diagnosis.detail if diagnosis is not None else None,
        remedy=diagnosis.remedy if diagnosis is not None else None))


def _reason_with_detail(base: str, diagnosis: StorageDiagnosis | None) -> str:
    """Причина с учётом вердикта: класс замещает общую формулировку, отрывок дополняет.

    Подстановка работает и для `SKIPPED_REASON`, потому что тот собран из
    `STORAGE_REASON`: «пропущено: хранилище не отвечает» превращается в
    «пропущено: базы данных не существует», а не теряет отметку о пропуске.
    """
    if diagnosis is None:
        return base
    if diagnosis.detail:
        return base.replace(STORAGE_REASON, DETAIL_REASONS[diagnosis.detail])
    if diagnosis.redacted:
        return f"{base}: {diagnosis.redacted}"
    return base


def _safe(payload: dict, section: str, produce, default, reason: str,
          state: _StorageState, backend: str = BACKEND_POSTGRES):
    """Собрать секцию fail-open: сбой → default + запись в gaps.

    `backend` — хранилище секции; дефолт Postgres, потому что своё хранилище он
    у всех секций, кроме `related.linked`. При взведённом флаге ЭТОГО бэкенда
    источник не вызывается вовсе — секция получает свой default и запись
    о пропуске, поэтому payload по-прежнему содержит все ключи `SECTIONS`.

    Какой бэкенд упал, решает тип исключения, а не разметка секции: она отвечает
    на другой вопрос — кого пропускать.
    """
    if section in _EMBEDDER_SECTIONS and state.is_down(SOURCE_EMBEDDER):
        _source_gap(payload, section, state, SOURCE_EMBEDDER, skipped=True)
        return default
    if state.is_down(backend):
        _source_gap(payload, section, state, backend, skipped=True)
        return default
    try:
        return produce()
    except Exception as exc:  # noqa: BLE001 — источник секции недоступен, это штатный случай
        log.warning("prepare_task_context: секция %s недоступна", section, exc_info=True)
        if is_embedder_unavailable(exc):
            state.mark(SOURCE_EMBEDDER, exc)
            _source_gap(payload, section, state, SOURCE_EMBEDDER, skipped=False)
        elif is_storage_unavailable(exc):
            failed = storage_backend(exc) or backend
            state.mark(failed, exc)
            _source_gap(payload, section, state, failed, skipped=False)
        else:
            payload["gaps"].append(gap(section, reason))
        return default


def _absorb_graph_error(preflight, state: _StorageState):
    """Взвести флаг графа по ошибке, которую preflight проглотил внутри себя.

    Preflight обязан остаться собранной секцией (`graph_nodes=None` — валидная
    деградация, на ней стоит CLI `status`), поэтому исключение приходит не
    броском, а ключом словаря. Ключ извлекается: payload читает LLM, объекту
    исключения там места нет.

    Упавший preflight (None вместо словаря) и провайдер без этого ключа проходят
    функцию без изменений.
    """
    if not isinstance(preflight, dict):
        return preflight
    error = preflight.pop("graph_error", None)
    if error is not None and is_storage_unavailable(error):
        state.mark(BACKEND_GRAPH, error)
    return preflight


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


def _endpoints(deps) -> tuple[str, ...]:
    """Эндпоинты хранилищ, если провайдер умеет их назвать.

    Читается через `getattr`, как `augment_gaps`: модуль намеренно не знает про
    Settings, а старый провайдер без этого метода обязан продолжать работать.
    """
    getter = getattr(deps, "storage_endpoints", None)
    if not callable(getter):
        return ()
    try:
        return tuple(getter() or ())
    except Exception:  # noqa: BLE001 — источник эндпоинтов недоступен, это не повод падать
        log.warning("prepare_task_context: эндпоинты хранилищ недоступны", exc_info=True)
        return ()


def build_task_context(deps, *, repo: str, key: str, branch: str,
                       warm_board: bool = True) -> dict:
    """Единый payload контекста задачи. Ни один сбой секции не прерывает сборку."""
    payload: dict = {section: None for section in SECTIONS}
    payload["gaps"] = []
    payload["warnings"] = []
    state = _StorageState(_endpoints(deps))

    payload["preflight"] = _absorb_graph_error(
        _safe(payload, "preflight", lambda: deps.preflight(repo, branch), None,
              "статус индекса недоступен", state),
        state)
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
        if state.is_down(BACKEND_POSTGRES):
            _source_gap(payload, "warm_board", state, BACKEND_POSTGRES, skipped=True)
        else:
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
                        state, BACKEND_GRAPH),
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
