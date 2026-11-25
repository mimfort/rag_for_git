"""prepare_task_context: форма payload и посекционный fail-open (PRI-248)."""
from unittest.mock import MagicMock

import psycopg
import psycopg_pool
import pytest
from neo4j.exceptions import ServiceUnavailable

from reviewer import storage_health as sh
from reviewer.config.settings import Settings
from reviewer.mcp import task_context
from reviewer.mcp.service import MCPReviewService, _TaskContextDeps
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import TaskHit


@pytest.fixture(autouse=True)
def _live_server_probe(monkeypatch):
    """Проба ветки PoolTimeout не должна открывать сокет в unit-тесте.

    Дефолт — «сервер отвечает», то есть пул действительно занят. Тест, которому
    нужен остановленный контейнер, подменяет пробу своей, падающей.
    """
    monkeypatch.setattr(sh, "_probe_postgres", lambda dsn: None)


class FakeDeps:
    """Фейковые провайдеры секций. Любое поле-исключение имитирует сбой источника."""

    def __init__(self, **overrides):
        self.calls = []
        self._overrides = overrides

    def _result(self, name, default):
        value = self._overrides.get(name, default)
        self.calls.append(name)
        if isinstance(value, Exception):
            raise value
        return value

    def preflight(self, repo, branch):
        payload = self._result("preflight", {
            "branch": branch, "indexed_sha": "abc", "drift": 0,
            "summaries": 40, "chunks": 7110, "graph_nodes": 7362})
        if isinstance(payload, dict) and "graph_error" in self._overrides:
            payload = {**payload, "graph_error": self._overrides["graph_error"]}
        return payload

    def storage_endpoints(self):
        return ("postgresql://u:p@127.0.0.1:5433/reviewer", "bolt://localhost:7687")

    def task_board(self, repo, branch):
        return self._result("task_board", {
            "type": "yougile", "project": "PRI", "key_pattern": r"PRI-\d+",
            "create_target": None, "done_target": "Готово", "options": {}})

    def warm_board(self, repo, branch):
        return self._result("warm_board", {"enumerated": 109, "changed": 0})

    def task(self, key, project):
        return self._result("task", {"key": "ID-302", "title": "T", "description": "D"})

    def linked(self, key, project):
        return self._result("linked", "Task PRI-248\n  Linked tasks: ...")

    def similar(self, query, project, *, task=None):
        return self._result("similar", "1. ID-300 ...")

    def subsystems(self, repo, branch, query):
        return self._result("subsystems", {"summaries": [{"cluster_key": "reviewer/mcp"}]})

    def code(self, repo, branch, queries):
        return self._result("code", "reviewer/mcp/service.py#X (service.py:1-10)")

    def test_exemplars(self, repo, branch, queries):
        return self._result("test_exemplars", "tests/mcp/test_x.py#y")


def test_payload_has_all_sections():
    payload = task_context.build_task_context(
        FakeDeps(), repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    for section in task_context.SECTIONS:
        assert section in payload, section


def test_happy_path_has_no_gaps():
    payload = task_context.build_task_context(
        FakeDeps(), repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    assert payload["gaps"] == []
    assert payload["preflight"]["drift"] == 0
    assert payload["task"]["key"] == "ID-302"
    assert payload["related"]["linked"].startswith("Task PRI-248")
    assert payload["related"]["similar"].startswith("1. ID-300")


def test_warm_board_false_skips_sync():
    deps = FakeDeps()
    task_context.build_task_context(
        deps, repo="o/n", key="PRI-248", branch="dev", warm_board=False)
    assert "warm_board" not in deps.calls


def test_related_is_not_deduped_by_the_tool():
    """Дедуп linked ∪ similar — суждение LLM, тул отдаёт обе выдачи как есть."""
    payload = task_context.build_task_context(
        FakeDeps(), repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    assert set(payload["related"]) == {"linked", "similar"}


def _gap_sections(payload):
    return {g["section"] for g in payload["gaps"]}


def test_board_disabled_keeps_task_from_store():
    payload = task_context.build_task_context(
        FakeDeps(task_board=None), repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    assert payload["task_board"] is None
    assert payload["task"]["key"] == "ID-302"
    assert "warm_board" in _gap_sections(payload)


def test_board_unreachable_still_builds_payload():
    payload = task_context.build_task_context(
        FakeDeps(warm_board=RuntimeError("401")), repo="o/n", key="PRI-248",
        branch="dev", warm_board=True)
    assert "warm_board" in _gap_sections(payload)
    assert payload["code"]


def test_postgres_down_empties_retrieval_sections():
    deps = FakeDeps(code=RuntimeError("no pg"), test_exemplars=RuntimeError("no pg"),
                    similar=RuntimeError("no pg"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-248", branch="dev", warm_board=False)
    assert payload["code"] == ""
    assert payload["test_exemplars"] == ""
    assert payload["related"]["similar"] == ""
    assert {"code", "test_exemplars", "related.similar"} <= _gap_sections(payload)


def test_neo4j_down_empties_linked_only():
    """Критерий 5: настоящее neo4j-исключение, а не RuntimeError."""
    payload = task_context.build_task_context(
        FakeDeps(linked=ServiceUnavailable("no routing servers")), repo="o/n",
        key="PRI-276", branch="dev", warm_board=False)
    assert payload["related"]["linked"] == ""
    assert payload["related"]["similar"]
    entry = next(g for g in payload["gaps"] if g["section"] == "related.linked")
    assert entry["cause"] == "storage_unavailable"
    assert entry["remedy"] == "reviewer start"


def test_neo4j_down_keeps_postgres_sections():
    """Критерий 4: теряется ровно related.linked, Postgres-секции собраны полностью."""
    deps = FakeDeps(linked=ServiceUnavailable("no routing servers"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    assert payload["code"] and payload["test_exemplars"]
    assert payload["subsystems"] and payload["related"]["similar"]
    assert [g["section"] for g in payload["gaps"]] == ["related.linked"]


def test_both_stores_down_keep_separate_diagnoses():
    """Вердикты не сливаются: причина Postgres не приписывается графу."""
    deps = FakeDeps(
        preflight=psycopg.OperationalError(
            'connection failed: FATAL:  password authentication failed for user "reviewer"'),
        linked=ServiceUnavailable("no routing servers"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    pg_entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    graph_entry = next(g for g in payload["gaps"] if g["section"] == "related.linked")
    assert pg_entry["cause_detail"] == "auth_failed"
    assert graph_entry["cause_detail"] is None


def test_no_index_marks_gap_and_keeps_going():
    payload = task_context.build_task_context(
        FakeDeps(preflight=RuntimeError("no index")), repo="o/n", key="PRI-248",
        branch="dev", warm_board=False)
    assert payload["preflight"] is None
    assert "preflight" in _gap_sections(payload)
    assert payload["code"]


def test_no_summaries_marks_gap():
    payload = task_context.build_task_context(
        FakeDeps(subsystems=RuntimeError("нет сводок")), repo="o/n", key="PRI-248",
        branch="dev", warm_board=False)
    assert payload["subsystems"] is None
    assert "subsystems" in _gap_sections(payload)


def test_task_missing_is_a_gap_not_an_error():
    payload = task_context.build_task_context(
        FakeDeps(task=None), repo="свободный текст задачи", key="свободный текст задачи",
        branch="dev", warm_board=False)
    assert payload["task"] is None
    assert "task" in _gap_sections(payload)


@pytest.mark.parametrize("key", ["PRI-295", "ID-388", "свободный текст"])
def test_context_similar_filters_current_task_before_render_and_augment(key, monkeypatch):
    task = {"key": "ID-388", "aliases": ["PRI-295", "BOARD-295"], "title": "T"}
    neighbors = [TaskHit(f"ID-{i}", "T", "open", 0.02, [f"PRI-{i}"])
                 for i in range(1, 9)]
    current = TaskHit("ID-388", "T", "open", 0.03, ["PRI-295"])
    alias_hit = TaskHit("BOARD-295", "T", "open", 0.02)
    hit_with_alias = TaskHit("LEGACY-1", "T", "open", 0.02, ["BOARD-295"])
    hits = [current, neighbors[0], alias_hit, hit_with_alias, *neighbors[1:]]

    store = MagicMock()
    store.search.return_value = hits
    embedder = MagicMock()
    svc = MagicMock()
    svc.components.task_service = TaskService(store, None, embedder)
    resolved_task = task if key in ("PRI-295", "ID-388") else None
    svc.get_task.return_value = resolved_task
    history = svc._review_service._ensure_history.return_value
    history.diff_paths_for_tasks.return_value = {
        "PRI-295": ["own.py"],
        "BOARD-295": ["own-alias.py"],
        "PRI-1": ["neighbor.py"],
    }

    deps = _TaskContextDeps(svc, None)
    monkeypatch.setattr(deps, "preflight", lambda repo, branch: {})
    monkeypatch.setattr(deps, "task_board", lambda repo, branch: {"project": "PRI"})
    payload = task_context.build_task_context(
        deps, repo="o/n", key=key, branch="dev", warm_board=False)

    expected_hits = neighbors if resolved_task else hits
    rendered_keys = [line.split()[1] for line in payload["related"]["similar"].splitlines()
                     if line and line[0].isdigit()]
    assert rendered_keys == [hit.key for hit in expected_hits[:8]]
    embedder.embed_query.assert_called_once_with("T" if resolved_task else key)
    store.search.assert_called_once()
    history.diff_paths_for_tasks.assert_called_once_with(
        list(dict.fromkeys(identity for hit in expected_hits
                           for identity in [hit.key, *hit.aliases])),
        repo="o/n",
    )
    sources = svc._search_codebase_multi.call_args_list[0].kwargs["augment_sources"]
    expected_paths = ["neighbor.py"] if resolved_task else [
        "own.py", "neighbor.py", "own-alias.py"]
    assert sources[0].paths == expected_paths


def test_board_less_query_falls_back_to_user_formulation():
    captured = {}

    class CapturingDeps(FakeDeps):
        def code(self, repo, branch, queries):
            captured["queries"] = queries
            return "snippet"

    task_context.build_task_context(
        CapturingDeps(task=None), repo="o/n", key="добавить logout endpoint",
        branch="dev", warm_board=False)
    assert captured["queries"] == ["добавить logout endpoint"]


def test_every_failure_still_returns_all_sections():
    deps = FakeDeps(preflight=RuntimeError(), task_board=RuntimeError(),
                    task=RuntimeError(), linked=RuntimeError(), similar=RuntimeError(),
                    subsystems=RuntimeError(), code=RuntimeError(),
                    test_exemplars=RuntimeError())
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-248", branch="dev", warm_board=True)
    for section in task_context.SECTIONS:
        assert section in payload
    assert len(payload["gaps"]) >= 8


def test_service_method_resolves_branch_and_delegates(monkeypatch):
    """Метод сервиса резолвит (repo, branch) и отдаёт payload модуля."""
    from reviewer.mcp import service as service_mod

    captured = {}

    def fake_build(deps, *, repo, key, branch, warm_board):
        captured.update(repo=repo, key=key, branch=branch, warm_board=warm_board)
        return {"preflight": {"branch": branch}, "gaps": []}

    monkeypatch.setattr(service_mod.task_context, "build_task_context", fake_build)

    svc = service_mod.MCPReviewService.__new__(service_mod.MCPReviewService)
    monkeypatch.setattr(
        service_mod.MCPReviewService, "_resolve_repo_branch",
        lambda self, repo, branch: ("owner/name", "dev"))
    payload = svc.prepare_task_context("owner/name", "PRI-248", branch="dev")
    assert captured["repo"] == "owner/name"
    assert captured["branch"] == "dev"
    assert payload["preflight"]["branch"] == "dev"


def test_service_method_returns_gap_on_bad_repo(monkeypatch):
    """Нерезолвящийся repo/branch — не исключение, а payload с пробелом."""
    from reviewer.mcp import service as service_mod

    svc = service_mod.MCPReviewService.__new__(service_mod.MCPReviewService)
    monkeypatch.setattr(
        service_mod.MCPReviewService, "_resolve_repo_branch",
        lambda self, repo, branch: "(repo не задан: передайте repo или задайте DEFAULT_REPO)")
    payload = svc.prepare_task_context("", "PRI-248")
    assert payload["task_board"] is None
    assert any(g["section"] == "repo" for g in payload["gaps"])


def test_code_section_receives_subquery_list():
    """Секция code ищется набором подзапросов, а не одной строкой."""
    seen = {}

    class Deps(FakeDeps):
        def code(self, repo, branch, queries):
            seen["code"] = queries
            return "reviewer/a.py#f (reviewer/a.py:1-3)"

        def test_exemplars(self, repo, branch, queries):
            seen["tests"] = queries
            return "tests/test_a.py#t"

    task_context.build_task_context(
        Deps(task={"key": "ID-1", "title": "T",
                   "description": "## Что сделать\n\n1. первый пункт\n2. второй пункт\n"}),
        repo="o/n", key="PRI-255", branch="dev", warm_board=False)

    assert isinstance(seen["code"], list)
    assert seen["code"][0] == task_context._query(
        {"key": "ID-1", "title": "T",
         "description": "## Что сделать\n\n1. первый пункт\n2. второй пункт\n"}, "PRI-255")
    assert any("второй пункт" in q for q in seen["code"])
    assert all(q.startswith("как тестируется: ") for q in seen["tests"])


def test_board_less_task_degenerates_to_single_query():
    """Без задачи в сторе набор равен одному запросу — поведение как прежде."""
    seen = {}

    class Deps(FakeDeps):
        def code(self, repo, branch, queries):
            seen["code"] = queries
            return ""

        def test_exemplars(self, repo, branch, queries):
            return ""

    task_context.build_task_context(Deps(task=None), repo="o/n",
                                    key="добавить эндпоинт логаута",
                                    branch="dev", warm_board=False)
    assert seen["code"] == ["добавить эндпоинт логаута"]


def test_test_queries_first_element_has_test_prefix_over_production_query():
    """Первый элемент _test_queries — продакшн-запрос с префиксом "как тестируется: "."""
    task = {"title": "T", "description": "D"}
    first = task_context._test_queries(task_context._queries(task, "PRI-1"))[0]
    assert first == f"как тестируется: {task_context._query(task, 'PRI-1')}"


# ---------------------------------------------------------------------------
# Тесты Task 4 (PRI-257): проводка augmented-сигналов в build_task_context
# ---------------------------------------------------------------------------

def test_similar_runs_before_code_so_hits_are_available():
    """Порядок вызовов — контракт: ключи секции code берутся из хитов similar."""
    deps = FakeDeps()
    task_context.build_task_context(deps, repo="o/n", key="ID-311", branch="dev",
                                    warm_board=False)
    assert deps.calls.index("similar") < deps.calls.index("code")


def test_augment_gaps_are_copied_into_payload():
    deps = FakeDeps()
    deps.augment_gaps = ["git-история недоступна: CalledProcessError"]
    payload = task_context.build_task_context(deps, repo="o/n", key="ID-311",
                                              branch="dev", warm_board=False)
    assert payload["code"], "сбой подмешивания не обнуляет секцию"
    entry = next(g for g in payload["gaps"] if g["section"] == "code.augment")
    assert entry["reason"] == "git-история недоступна: CalledProcessError"
    assert entry["cause"] == task_context.CAUSE_UNKNOWN
    assert entry["remedy"] is None


def test_deps_without_augment_gaps_attribute_still_work():
    """Старый провайдер секций (без нового поля) не должен падать."""
    payload = task_context.build_task_context(FakeDeps(), repo="o/n", key="ID-311",
                                              branch="dev", warm_board=False)
    assert payload["gaps"] == []


def test_similar_section_text_is_unchanged_by_augmentation():
    """related.similar остаётся тем же текстом независимо от пробелов подмешивания в code.

    Брифовский вариант этого теста опирается на несуществующую фикстуру
    fake_deps_factory/deps.expected_similar_text (в репозитории её нет —
    ни conftest.py, ни такого поля у FakeDeps). Проверяем тот же инвариант
    напрямую: текст related.similar не зависит от augment_gaps секции code.
    """
    deps = FakeDeps()
    deps.augment_gaps = ["git-история недоступна: CalledProcessError"]
    payload = task_context.build_task_context(deps, repo="o/n", key="ID-311",
                                              branch="dev", warm_board=False)
    assert payload["related"]["similar"] == "1. ID-300 ..."


# ---------------------------------------------------------------------------
# Тесты Task 2 (PRI-268): класс причины в gaps и короткое замыкание секций
# ---------------------------------------------------------------------------

def test_storage_failure_names_cause_and_remedy():
    """Критерий 1: в gaps названы и причина, и команда лечения."""
    deps = FakeDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "storage_unavailable"
    assert entry["remedy"] == "reviewer start"


def test_other_failure_keeps_cause_unknown():
    """Критерий 2: «хранилище не отвечает» и прочий сбой — разные записи."""
    deps = FakeDeps(preflight=RuntimeError("no index"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "unknown"
    assert entry["remedy"] is None


def test_storage_failure_short_circuits_remaining_sections():
    """Критерий 1 PRI-268: Postgres-секции не вызываются — иначе +30 с каждая.

    related.linked живёт в другом хранилище и Postgres-сбоем не замыкается
    (PRI-276): при живом Neo4j секция собирается, вместо того чтобы теряться зря.
    """
    deps = FakeDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    assert deps.calls == ["preflight", "linked"]
    assert set(payload) == set(task_context.SECTIONS)
    assert all(g["cause"] == "storage_unavailable" for g in payload["gaps"])


def test_short_circuit_does_not_fire_on_other_causes():
    """Сбой не-хранилища остальные секции не отменяет: fail-open как прежде."""
    deps = FakeDeps(preflight=RuntimeError("no index"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    assert "code" in deps.calls
    assert payload["code"]


def test_remote_deploy_gets_cause_without_remedy():
    """Критерий 3: совет reviewer start не выдаётся удалённым эндпоинтам."""
    class RemoteDeps(FakeDeps):
        def storage_endpoints(self):
            return ("postgresql://u:p@db.example.com:5432/reviewer",
                    "bolt://neo4j.internal:7687")

    deps = RemoteDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "storage_unavailable"
    assert entry["remedy"] is None


def test_deps_without_storage_endpoints_still_work():
    """Провайдер без нового метода не ломает сборку — remedy просто пуст."""
    class OldDeps(FakeDeps):
        storage_endpoints = None

    deps = OldDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "storage_unavailable"
    assert entry["remedy"] is None


def test_existing_gaps_keep_section_and_reason():
    """Расширение аддитивно: прежние ключи записи на месте, добавлен cause_detail."""
    payload = task_context.build_task_context(
        FakeDeps(subsystems=RuntimeError("нет сводок")), repo="o/n",
        key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "subsystems")
    assert entry["reason"] == "сводки подсистем недоступны"
    assert set(entry) == {"section", "reason", "cause", "cause_detail", "remedy"}


def test_warm_board_gap_reflects_storage_down_not_misconfigured_board():
    """Найдено ревью: task_board упал по вине хранилища — warm_board не должен

    лгать «доска не настроена». Дефолт warm_board=True (как в живом MCP-туле):
    доска, возможно, настроена прекрасно, просто её конфиг не прочитался.
    """
    deps = FakeDeps(task_board=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=True)
    entry = next(g for g in payload["gaps"] if g["section"] == "warm_board")
    assert entry["cause"] == task_context.CAUSE_STORAGE_UNAVAILABLE
    # PRI-277: нераспознанный сбой дописывает вымаранный отрывок к SKIPPED_REASON
    # (см. test_unrecognised_failure_carries_redacted_excerpt) — поэтому не
    # точное равенство, а префикс.
    assert entry["reason"].startswith(task_context.SKIPPED_REASON)
    assert entry["remedy"] == "reviewer start"


def test_warm_board_gap_keeps_misconfigured_reason_without_storage_failure():
    """Регрессионный якорь: board-less без сбоя хранилища — прежний текст и cause unknown."""
    payload = task_context.build_task_context(
        FakeDeps(task_board=None), repo="o/n", key="PRI-268", branch="dev", warm_board=True)
    entry = next(g for g in payload["gaps"] if g["section"] == "warm_board")
    assert entry["reason"] == "доска не настроена"
    assert entry["cause"] == task_context.CAUSE_UNKNOWN


def test_broken_storage_endpoints_source_does_not_break_build():
    """Найдено ревью: сбой самого источника эндпоинтов (`_remedy`) не должен ронять сборку.

    `storage_endpoints()` может бросить (например, недоступен сам Settings) —
    `_remedy` обязан поймать, залогировать и отдать remedy=None, а не уронить
    build_task_context.
    """
    class BrokenEndpointsDeps(FakeDeps):
        def storage_endpoints(self):
            raise RuntimeError("эндпоинты недоступны")

    deps = BrokenEndpointsDeps(preflight=psycopg.OperationalError("connection refused"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-268", branch="dev", warm_board=False)
    assert set(payload) == set(task_context.SECTIONS)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == task_context.CAUSE_STORAGE_UNAVAILABLE
    assert entry["remedy"] is None


# ---------------------------------------------------------------------------
# Тесты PRI-277: класс причины отдельно от уместности лекарства
# ---------------------------------------------------------------------------

def _refuse_connection(dsn: str) -> None:
    """Проба остановленного контейнера: порт закрыт."""
    raise psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: Connection refused")


def _preflight_gap(exc):
    """Запись gaps секции preflight при заданном сбое источника."""
    payload = task_context.build_task_context(
        FakeDeps(preflight=exc), repo="o/n", key="PRI-277", branch="dev",
        warm_board=False)
    return next(g for g in payload["gaps"] if g["section"] == "preflight")


def test_auth_failure_is_named_and_loses_remedy():
    """Критерий 1 и 2: неверный пароль отличим и не получает совета."""
    entry = _preflight_gap(psycopg.OperationalError(
        'FATAL:  password authentication failed for user "reviewer"'))
    assert entry["cause"] == "storage_unavailable"      # критерий 4: не переклассифицируем
    assert entry["cause_detail"] == "auth_failed"
    assert entry["remedy"] is None
    assert "учётные данные" in entry["reason"]


def test_missing_database_is_named_and_loses_remedy():
    entry = _preflight_gap(psycopg.OperationalError(
        'FATAL:  database "nosuchdb" does not exist'))
    assert entry["cause"] == "storage_unavailable"
    assert entry["cause_detail"] == "missing_database"
    assert entry["remedy"] is None


def test_stopped_containers_still_get_remedy_and_no_detail():
    """Третий случай остаётся отличимым от первых двух с другой стороны."""
    entry = _preflight_gap(psycopg.OperationalError("Connection refused"))
    assert entry["cause_detail"] is None
    assert entry["remedy"] == "reviewer start"


def test_stopped_containers_keep_remedy_on_the_production_exception_type(monkeypatch):
    """Тот же случай на ПРОДОВОМ типе: пул отдаёт PoolTimeout, а не отказ соединения.

    Соседний тест выше моделирует остановленный контейнер голым
    `OperationalError`, которого прод по этому пути не производит: в Postgres
    он ходит только через пул. Именно этот зазор между фейком и продом пропустил
    дефект C1 — деталь `pool_exhausted` приписывалась лежачему хранилищу, и
    `reviewer start` исчезал.
    """
    monkeypatch.setattr(sh, "_probe_postgres", _refuse_connection)
    entry = _preflight_gap(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"))
    assert entry["cause"] == "storage_unavailable"
    assert entry["cause_detail"] is None
    assert entry["remedy"] == "reviewer start"


def test_unrecognised_failure_carries_redacted_excerpt():
    """Нераспознанный сбой перестаёт быть немым, но секретов не несёт."""
    entry = _preflight_gap(psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: no space left on device"))
    assert "no space left on device" in entry["reason"]
    assert "127.0.0.1" not in entry["reason"]


def test_skipped_sections_reuse_the_same_verdict():
    """Все записи одного прогона согласованы: вердикт считается один раз."""
    payload = task_context.build_task_context(
        FakeDeps(preflight=psycopg.OperationalError(
            'FATAL:  password authentication failed for user "reviewer"')),
        repo="o/n", key="PRI-277", branch="dev", warm_board=False)
    assert all(g["cause_detail"] == "auth_failed" for g in payload["gaps"])
    skipped = next(g for g in payload["gaps"] if g["section"] == "code")
    assert skipped["reason"].startswith("пропущено: ")
    assert skipped["remedy"] is None


def test_non_storage_gap_has_empty_detail():
    """Аддитивность: запись не про хранилище получает пятый ключ пустым."""
    payload = task_context.build_task_context(
        FakeDeps(subsystems=RuntimeError("нет сводок")), repo="o/n",
        key="PRI-277", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "subsystems")
    assert entry["cause"] == "unknown"
    assert entry["cause_detail"] is None


def test_reason_never_carries_password_without_storage_endpoints():
    """Найдено финальным ревью: без `storage_endpoints` `reason` не должен нести пароль.

    До фикса `classify_storage_failure` без эндпоинтов вымарывать было нечем, и
    `_redact` возвращал текст исключения как есть — пароль из conninfo уходил в
    `reason` записи `gaps` целиком (критерий 3 нарушался молча).
    """
    class OldDeps(FakeDeps):
        storage_endpoints = None

    exc = psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: Connection refused"
        " (conninfo: postgresql://reviewer:s3cretpw@127.0.0.1:5433/reviewer)")
    deps = OldDeps(preflight=exc)
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-277", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert "s3cretpw" not in entry["reason"]
    assert entry["cause_detail"] is None
    assert entry["remedy"] is None


# ---------------------------------------------------------------------------
# Тесты PRI-276: preflight взводит флаг графа, не платя второй таймаут
# ---------------------------------------------------------------------------

def test_graph_error_from_preflight_skips_linked_without_calling_graph():
    """Критерий 3: второй заход в мёртвый граф не делается вовсе."""
    deps = FakeDeps(graph_error=ServiceUnavailable("no routing servers"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    assert "linked" not in deps.calls
    entry = next(g for g in payload["gaps"] if g["section"] == "related.linked")
    assert entry["cause"] == "storage_unavailable"
    assert payload["related"]["linked"] == ""
    assert payload["code"] and payload["test_exemplars"]


def test_graph_error_does_not_leak_into_payload():
    """Объекту исключения в payload делать нечего: его читает LLM."""
    deps = FakeDeps(graph_error=ServiceUnavailable("no routing servers"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    assert "graph_error" not in payload["preflight"]
    assert payload["preflight"]["drift"] == 0


def test_non_storage_graph_error_is_ignored():
    """Не всякая ошибка графа — недоступность: замыкать нечего, секция вызывается."""
    deps = FakeDeps(graph_error=RuntimeError("cypher blew up"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-276", branch="dev", warm_board=False)
    assert "linked" in deps.calls
    assert payload["related"]["linked"]


# ---------------------------------------------------------------------------
# Тесты PRI-275: preflight не платит второй таймаут пула за мёртвый Postgres
# ---------------------------------------------------------------------------

def test_storage_down_makes_single_store_call_and_keeps_classification(monkeypatch):
    """build_task_context с НАСТОЯЩИМ _TaskContextDeps поверх фейкового components.store.

    До PRI-275 проглоченный сбой clone_path заставлял preflight читать
    get_index_meta_row/count_chunks ПОСЛЕ уже упавшего get_repo_clone — второй
    таймаут пула. Теперь strict=True на _repo_clone_path пробрасывает сбой
    до этих вызовов, и в стор делается ровно один заход.

    Сервис — настоящий MCPReviewService (а не MagicMock целиком): иначе
    _repo_clone_path был бы автосгенерированным моком без реальной strict-логики,
    и тест проверял бы не тот код, ради которого написан.

    Лежачее хранилище обязано получить `reviewer start`: сбой приходит продовым
    типом `PoolTimeout` (в Postgres прод ходит только через пул), а проба сервера
    показывает закрытый порт. Правка этого ожидания на `remedy is None` при
    реализации PRI-274 и была тем, что скрыло дефект C1: тест выглядел как
    согласование, а на деле снял единственную проверку лекарства на продовом типе.
    """
    calls: list[str] = []

    class _CountingStore:
        def get_repo_clone(self, repo: str) -> str | None:
            calls.append("get_repo_clone")
            raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")

        def get_index_meta_row(self, repo: str, ref: str):
            calls.append("get_index_meta_row")
            return None

        def count_chunks(self, repo: str, ref: str) -> int:
            calls.append("count_chunks")
            return 0

        def list_refs(self, repo: str) -> list[str]:
            calls.append("list_refs")
            return []

    monkeypatch.setattr(sh, "_probe_postgres", _refuse_connection)
    settings = Settings()
    # loopback-эндпоинты обязательны, иначе classify_storage_failure не назовёт
    # remedy: storage_endpoints() читает их из settings.pg_dsn/neo4j_uri.
    settings.pg_dsn = "postgresql://u:p@127.0.0.1:5433/reviewer"
    settings.neo4j_uri = "bolt://localhost:7687"
    components = MagicMock()
    components.store = _CountingStore()
    service = MCPReviewService(settings, components)
    deps = _TaskContextDeps(service, None)

    payload = task_context.build_task_context(
        deps, repo="o/r", key="PRI-275", branch="dev", warm_board=False)

    assert calls == ["get_repo_clone"]
    for section in task_context.SECTIONS:
        assert section in payload
    entry = next(g for g in payload["gaps"] if g["section"] == "preflight")
    assert entry["cause"] == "storage_unavailable"
    assert entry["cause_detail"] is None
    assert entry["remedy"] == "reviewer start"


# ---------------------------------------------------------------------------
# Тесты Task 2 (PRI-274/PRI-272): замыкание обобщается с хранилищ на источники
# ---------------------------------------------------------------------------

def _build(**overrides):
    return task_context.build_task_context(
        FakeDeps(**overrides), repo="o/n", key="PRI-274", branch="dev",
        warm_board=True)


def _gap(payload, section):
    return next(g for g in payload["gaps"] if g["section"] == section)


def test_embedder_failure_gets_own_cause():
    """Сбой эмбеддера — свой класс, не storage_unavailable и не unknown."""
    from voyageai.error import APIError

    payload = _build(subsystems=APIError("HTTP code 403"))

    entry = _gap(payload, "subsystems")
    assert entry["cause"] == "embedder_unavailable"
    assert entry["remedy"] is None
    assert entry["cause_detail"] is None
    assert "эмбеддер" in entry["reason"]


def test_embedder_failure_does_not_close_storage_sections():
    """Мёртвый эмбеддер не отменяет секции, которым нужен только Postgres."""
    from voyageai.error import APIError

    payload = _build(subsystems=APIError("HTTP code 403"))

    assert payload["task_board"] is not None
    assert payload["task"] is not None
    assert "related.linked" not in _gap_sections(payload)


def test_embedder_closure_skips_later_embedder_sections():
    """Второй заход в мёртвый эмбеддер не делается: секция пропускается."""
    from voyageai.error import APIError

    deps = FakeDeps(similar=APIError("HTTP code 403"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-274", branch="dev", warm_board=True)

    assert "code" not in deps.calls
    assert "test_exemplars" not in deps.calls
    entry = _gap(payload, "code")
    assert entry["cause"] == "embedder_unavailable"
    assert entry["reason"].startswith("пропущено")


def test_embedder_closure_does_not_skip_postgres_sections():
    """Замыкание адресное: секции, которым эмбеддер не нужен, всё равно идут."""
    from voyageai.error import APIError

    deps = FakeDeps(similar=APIError("HTTP code 403"))
    task_context.build_task_context(
        deps, repo="o/n", key="PRI-274", branch="dev", warm_board=True)

    assert "task" in deps.calls
    assert "linked" in deps.calls
    assert deps.calls.count("similar") == 1


def test_pool_exhaustion_reports_detail_and_no_remedy():
    """Исчерпание пула: класс storage_unavailable, но лекарство не советуется.

    Пул назван занятым только потому, что проба (фикстура `_live_server_probe`)
    соединилась с сервером; на закрытом порту тот же тип исключения даёт другой
    вердикт — см. `test_stopped_containers_keep_remedy_on_the_production_exception_type`.
    """
    entry = _preflight_gap(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"))

    assert entry["cause"] == "storage_unavailable"
    assert entry["cause_detail"] == "pool_exhausted"
    assert entry["remedy"] is None
    assert "пул" in entry["reason"]


# ---------------------------------------------------------------------------
# Тесты Task 5 (PRI-274/PRI-272): секция task берёт причину из синка
# ---------------------------------------------------------------------------

def test_task_gap_blames_embedder_not_the_board():
    """Задачи нет в сторе, потому что её туда не пустил упавший синк.

    Секция `task` не бросает вовсе, поэтому проброс её не лечит: причина
    рождается этажом выше, в своде warm_board.
    """
    payload = _build(
        warm_board={"enumerated": 126, "changed": 3, "failed": 3,
                    "embedder_failed": True},
        task=None)

    entry = _gap(payload, "task")
    assert entry["cause"] == "embedder_unavailable"
    assert "эмбеддер" in entry["reason"]
    assert "нет в сторе" not in entry["reason"]


def test_task_gap_stays_board_shaped_without_embedder_failure():
    """Без сбоя эмбеддера формулировка прежняя — задачи действительно нет."""
    payload = _build(
        warm_board={"enumerated": 126, "changed": 0, "failed": 0,
                    "embedder_failed": False},
        task=None)

    entry = _gap(payload, "task")
    assert entry["cause"] == "unknown"
    assert entry["reason"] == "задачи нет в сторе"


def test_legacy_warm_board_summary_without_the_flag_still_works():
    """Свод без нового ключа (старый деплой) проходит без исключения."""
    payload = _build(warm_board={"enumerated": 10, "changed": 0}, task=None)
    assert _gap(payload, "task")["reason"] == "задачи нет в сторе"


# ---------------------------------------------------------------------------
# M5 (финальное ревью PRI-274/272): имена в _EMBEDDER_SECTIONS не выдуманы
# ---------------------------------------------------------------------------

def _sections_passed_to_safe(monkeypatch) -> set[str]:
    """Имена секций, реально доехавшие до `_safe` за один сбор контекста."""
    seen: set[str] = set()
    original = task_context._safe

    def spy(payload, section, produce, default, reason, state,
            backend=sh.BACKEND_POSTGRES, **kwargs):
        seen.add(section)
        return original(payload, section, produce, default, reason, state, backend, **kwargs)

    monkeypatch.setattr(task_context, "_safe", spy)
    task_context.build_task_context(
        FakeDeps(), repo="o/n", key="PRI-272", branch="dev", warm_board=True)
    return seen


def test_embedder_sections_are_real_section_names(monkeypatch):
    """Опечатка в наборе тихо отключила бы замыкание для этой секции.

    Регрессия была бы невидима: payload остался бы правильным, вернулся бы лишь
    лишний таймаут Voyage — ни тест, ни глаз этого не поймали бы. Поэтому набор
    сверяется не с копией литерала, а с фактическими именами, которые
    `build_task_context` передаёт в `_safe`.
    """
    seen = _sections_passed_to_safe(monkeypatch)
    unknown = task_context._EMBEDDER_SECTIONS - seen
    assert not unknown, f"секций с такими именами не существует: {unknown}"


def test_every_embedder_section_is_actually_skipped_when_voyage_is_down():
    """Обратная сторона: каждое имя из набора действительно замыкается.

    Первая проверка ловит опечатку, эта — молчаливую потерю разметки: имя может
    существовать и всё равно не участвовать в замыкании, если ветка `_safe`
    разъедется с набором.
    """
    from voyageai.error import APIError

    payload = task_context.build_task_context(
        FakeDeps(similar=APIError("HTTP code 403")), repo="o/n", key="PRI-272",
        branch="dev", warm_board=True)
    skipped = {g["section"] for g in payload["gaps"]
               if g["cause"] == "embedder_unavailable"}
    assert task_context._EMBEDDER_SECTIONS <= skipped


def test_context_reports_criteria_that_cannot_fit_in_either_search():
    criteria = [f"criterion_{i}" for i in range(21)]
    seen = {}

    class Deps(FakeDeps):
        def code(self, repo, branch, queries):
            seen["code"] = queries
            return "code"

        def test_exemplars(self, repo, branch, queries):
            seen["tests"] = queries
            return "tests"

    payload = task_context.build_task_context(
        Deps(task={"key": "ID-1", "title": "T", "criteria": criteria}),
        repo="o/n", key="PRI-294", branch="dev", warm_board=False)

    assert seen["code"] == ["T", *criteria[:19]]
    assert seen["tests"] == [f"как тестируется: {query}" for query in seen["code"]]
    assert payload["warnings"] == [
        {"subqueries": {"excluded_criteria": criteria[19:], "reason": "subquery_budget"}},
    ]
    assert payload["gaps"] == []


def test_context_keeps_paths_in_both_searches_when_query_budget_is_full():
    from reviewer.mcp.subqueries import MAX_SUBQUERIES

    filenames = (".review.yml", "plugin/skills/configure-review/SKILL.md", "pyproject.toml")
    steps = "\n".join(f"- Шаг {i}" for i in range(MAX_SUBQUERIES))
    text = "## Требования\n" + steps + "\n\n## Контекст\n\n" + " ".join(f"`{name}`" for name in filenames)
    criteria = ["Проверить тайм-аут после запуска.", "Отчёт показывает текущую ветку."]
    seen = {}

    class Deps(FakeDeps):
        def code(self, repo, branch, queries):
            seen["code"] = queries
            return "code"

        def test_exemplars(self, repo, branch, queries):
            seen["tests"] = queries
            return "tests"

    task_context.build_task_context(
        Deps(task={"key": "ID-1", "title": "T", "description": text, "criteria": criteria}),
        repo="o/n", key="PRI-309", branch="dev", warm_board=False,
    )

    assert len(seen["code"]) == MAX_SUBQUERIES
    assert seen["code"][0] == task_context._query({"title": "T", "description": text}, "PRI-309")
    assert all(criterion in seen["code"] for criterion in criteria)
    assert set(filenames) <= set(" ".join(seen["code"]).split())
    assert seen["tests"] == [f"как тестируется: {query}" for query in seen["code"]]


@pytest.mark.parametrize("outage", ["similar", "code", "test_exemplars", "postgres", "bad_query", None])
def test_task_retrieval_keeps_bm25_when_vector_provider_is_unavailable(monkeypatch, outage):
    from types import SimpleNamespace

    from voyageai.error import APIError

    from reviewer.index.store import Retrieved
    from reviewer.policy.context_limits import ContextLimits

    def hit(path, symbol):
        return Retrieved(f"{path}#{symbol}", path, symbol, "function", 1, 1,
                         f"def {symbol}(): pass", 1.0, bm25_hit=True)

    embedder = MagicMock()
    embedder.embed_queries.side_effect = lambda queries: [[1.0] for _ in queries]
    store = MagicMock()
    hits = [hit("code.py", "example"), hit("tests/test_code.py", "test_example")]
    store.bm25_search.return_value = hits
    store.hybrid_search.return_value = hits
    components = MagicMock()
    components.retriever = SimpleNamespace(embedder=embedder, store=store, graph=None)
    service = MCPReviewService(Settings(), components)
    monkeypatch.setattr(service, "_resolve_repo_branch", lambda repo, branch: (repo, branch))
    monkeypatch.setattr(service, "_resolve_context_limits", lambda _repo, _branch: ContextLimits())
    monkeypatch.setattr(_TaskContextDeps, "preflight", lambda *_: {})
    monkeypatch.setattr(_TaskContextDeps, "task_board", lambda *_: None)
    monkeypatch.setattr(_TaskContextDeps, "task", lambda *_: None)
    monkeypatch.setattr(_TaskContextDeps, "linked", lambda *_: "")
    monkeypatch.setattr(_TaskContextDeps, "subsystems", lambda *_: None)
    monkeypatch.setattr(_TaskContextDeps, "_augment_paths", lambda *_: [])

    def similar(*_, **__):
        embedder.embed_query("query")
        return ""

    monkeypatch.setattr(_TaskContextDeps, "similar", similar)
    failure = APIError("HTTP code 403")
    if outage in {"similar", "postgres", "bad_query"}:
        embedder.embed_query.side_effect = failure
    elif outage == "code":
        embedder.embed_queries.side_effect = failure
    elif outage == "test_exemplars":
        embedder.embed_queries.side_effect = [[[1.0]], failure]
    if outage == "postgres":
        store.bm25_search.side_effect = psycopg.OperationalError("connection refused")
    elif outage == "bad_query":
        store.bm25_search.side_effect = psycopg.ProgrammingError("BM25 query error")

    payload = service.prepare_task_context("o/n", "query", "dev", warm_board=False)

    embedder.embed_query.assert_called_once_with("query")
    if outage == "postgres":
        store.bm25_search.assert_called_once()
        embedder.embed_queries.assert_not_called()
        assert payload["code"] == payload["test_exemplars"] == ""
        assert _gap(payload, "code.bm25")["cause"] == "storage_unavailable"
        assert _gap(payload, "test_exemplars.bm25")["reason"].startswith("пропущено")
        assert not payload["warnings"]
        return
    if outage == "bad_query":
        assert store.bm25_search.call_count == 2
        embedder.embed_queries.assert_not_called()
        assert payload["code"] == payload["test_exemplars"] == ""
        assert _gap(payload, "code.bm25")["cause"] == "unknown"
        assert not payload["warnings"]
        return

    assert "code.py#example" in payload["code"]
    assert "tests/test_code.py#test_example" not in payload["code"]
    assert "tests/test_code.py#test_example" in payload["test_exemplars"]
    assert "code.py#example" not in payload["test_exemplars"]
    expected = {
        "similar": ["code", "test_exemplars"],
        "code": ["code", "test_exemplars"],
        "test_exemplars": ["test_exemplars"],
        None: [],
    }[outage]
    assert payload["warnings"] == [
        {"retrieval": {"section": section, "mode": "bm25_only"}} for section in expected
    ]
    for section in expected:
        assert _gap(payload, f"{section}.semantic_search")["cause"] == "embedder_unavailable"
    assert embedder.embed_queries.call_count == {"similar": 0, "code": 1,
                                                  "test_exemplars": 2, None: 2}[outage]
    assert store.bm25_search.call_count == len(expected)
    for call in store.bm25_search.call_args_list:
        assert call.kwargs["base_ref"] == "base:dev"
        assert call.kwargs["top_k"] == ContextLimits().search_codebase.candidate_pool


@pytest.mark.parametrize("mode", ["hybrid", "bm25"])
@pytest.mark.parametrize("failure", [
    "preflight_graph", "graph", "hydration", "explicit", "augment", "bad_query", None,
])
def test_prepare_preserves_search_hits_when_an_auxiliary_source_fails(monkeypatch, mode, failure):
    from types import SimpleNamespace

    from voyageai.error import APIError

    from reviewer.index.store import Retrieved
    from reviewer.mcp.schemas import BriefCitationIn
    from reviewer.policy.context_limits import ContextLimits

    def hit(path):
        return Retrieved(f"{path}#example", path, "example", "function", 1, 1,
                         "def example(): pass", 1.0, bm25_hit=True)

    embedder = MagicMock()
    embedder.embed_queries.side_effect = lambda queries: [[1.0] for _ in queries]
    store = MagicMock()
    hits = [hit("a.py"), hit("tests/test_a.py")]
    store.bm25_search.return_value = store.hybrid_search.return_value = hits
    store.fetch_nodes.return_value = [hit("b.py"), hit("tests/test_b.py")]
    store.fetch_retrieved_at_paths.return_value = [hit("extra.py")]
    graph = MagicMock()
    graph.expand_detailed.return_value = [{"id": "b.py#example"}, {"id": "tests/test_b.py#example"}]
    graph_failure = ServiceUnavailable("no routing servers")
    if failure == "graph":
        graph.expand_detailed.side_effect = graph_failure
    elif failure == "hydration":
        store.fetch_nodes.side_effect = psycopg.OperationalError("connection refused")
    elif failure in {"explicit", "augment"}:
        store.fetch_retrieved_at_paths.side_effect = psycopg.OperationalError("connection refused")
    elif failure == "bad_query":
        store.fetch_nodes.side_effect = psycopg.ProgrammingError("incorrect hydration query")

    components = MagicMock()
    components.retriever = SimpleNamespace(embedder=embedder, store=store, graph=graph)
    service = MCPReviewService(Settings(), components)
    monkeypatch.setattr(service, "_resolve_repo_branch", lambda repo, branch: (repo, branch))
    monkeypatch.setattr(service, "_resolve_context_limits", lambda _repo, _branch: ContextLimits())
    monkeypatch.setattr(_TaskContextDeps, "preflight", lambda *_: {
        "indexed_sha": "snap",
        "graph_error": graph_failure if failure == "preflight_graph" else None,
    })
    monkeypatch.setattr(_TaskContextDeps, "task_board", lambda *_: None)
    title = "Проверить pkg/explicit.py" if failure == "explicit" else "пример"
    monkeypatch.setattr(_TaskContextDeps, "task", lambda *_: {"title": title})
    monkeypatch.setattr(service, "_resolve_policy",
                        lambda *_: (SimpleNamespace(ignore=[]), SimpleNamespace(skipped=())))
    monkeypatch.setattr(_TaskContextDeps, "linked", lambda *_: "")
    monkeypatch.setattr(_TaskContextDeps, "subsystems", lambda *_: None)
    monkeypatch.setattr(_TaskContextDeps, "_augment_paths", lambda *_: ["extra.py"])
    monkeypatch.setattr(_TaskContextDeps, "brief_sources",
                        lambda self, repo, sha, paths: {path: "def example(): pass" for path in paths})
    similar = MagicMock(return_value="")
    if mode == "bm25":
        similar.side_effect = APIError("HTTP code 403")
    monkeypatch.setattr(_TaskContextDeps, "similar", similar)

    payload = service.prepare_task_context("o/n", "query", "dev", warm_board=False,
                                           include_brief_evidence=True)
    search = store.bm25_search if mode == "bm25" else store.hybrid_search
    assert "a.py#example" in payload["code"]
    citation = BriefCitationIn(section="code", path="a.py", line=1)
    verdict = service.verify_task_brief(payload["brief_evidence_id"], citations=[citation])
    assert verdict["citations"][0]["status"] == "verified"

    if failure in {"hydration", "augment", "explicit"}:
        store.fetch_nodes.assert_called_once()
        if failure == "hydration":
            store.fetch_retrieved_at_paths.assert_not_called()
            category = "code.graph_chunks"
        elif failure == "explicit":
            store.fetch_retrieved_at_paths.assert_called_once_with(
                "o/n", ["pkg/explicit.py"], base_ref="base:dev")
            category = "code.explicit_paths"
        else:
            store.fetch_retrieved_at_paths.assert_called_once()
            assert "b.py#example" in payload["code"]
            category = "code.augment.similar-diffs"
        search.assert_called_once()
        assert payload["test_exemplars"] == ""
        assert _gap(payload, category)["cause"] == "storage_unavailable"
        tests_section = "test_exemplars.bm25" if mode == "bm25" else "test_exemplars"
        assert _gap(payload, tests_section)["reason"].startswith("пропущено")
        return

    assert search.call_count == 2
    assert "tests/test_a.py#example" in payload["test_exemplars"]
    store.fetch_retrieved_at_paths.assert_called_once()
    if failure == "graph":
        graph.expand_detailed.assert_called_once()
        store.fetch_nodes.assert_not_called()
        assert _gap(payload, "code.graph")["cause"] == "storage_unavailable"
        assert _gap(payload, "test_exemplars.graph")["reason"].startswith("пропущено")
    elif failure == "preflight_graph":
        graph.expand_detailed.assert_not_called()
        store.fetch_nodes.assert_not_called()
        assert _gap(payload, "code.graph")["reason"].startswith("пропущено")
    elif failure == "bad_query":
        assert store.fetch_nodes.call_count == 2
        assert _gap(payload, "code.graph_chunks")["cause"] == "unknown"
        assert _gap(payload, "test_exemplars.graph_chunks")["cause"] == "unknown"
    else:
        assert graph.expand_detailed.call_count == store.fetch_nodes.call_count == 2
        assert payload["code"].index("a.py#example") < payload["code"].index("b.py#example")
        assert "tests/test_b.py#example" in payload["test_exemplars"]
        assert not any(".graph" in gap["section"] for gap in payload["gaps"])
