"""prepare_task_context: форма payload и посекционный fail-open (PRI-248)."""
from unittest.mock import MagicMock

import psycopg
import psycopg_pool
from neo4j.exceptions import ServiceUnavailable

from reviewer.config.settings import Settings
from reviewer.mcp import task_context
from reviewer.mcp.service import MCPReviewService, _TaskContextDeps


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

    def similar(self, query, project):
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
    first = task_context._test_queries(task, "PRI-1")[0]
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

def test_storage_down_makes_single_store_call_and_keeps_classification():
    """build_task_context с НАСТОЯЩИМ _TaskContextDeps поверх фейкового components.store.

    До PRI-275 проглоченный сбой clone_path заставлял preflight читать
    get_index_meta_row/count_chunks ПОСЛЕ уже упавшего get_repo_clone — второй
    таймаут пула. Теперь strict=True на _repo_clone_path пробрасывает сбой
    до этих вызовов, и в стор делается ровно один заход.

    Сервис — настоящий MCPReviewService (а не MagicMock целиком): иначе
    _repo_clone_path был бы автосгенерированным моком без реальной strict-логики,
    и тест проверял бы не тот код, ради которого написан.
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
    assert entry["remedy"] == "reviewer start"
