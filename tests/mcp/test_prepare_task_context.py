"""prepare_task_context: форма payload и посекционный fail-open (PRI-248)."""
from reviewer.mcp import task_context


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
        return self._result("preflight", {
            "branch": branch, "indexed_sha": "abc", "drift": 0,
            "summaries": 40, "chunks": 7110, "graph_nodes": 7362})

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
    payload = task_context.build_task_context(
        FakeDeps(linked=RuntimeError("no neo4j")), repo="o/n", key="PRI-248",
        branch="dev", warm_board=False)
    assert payload["related"]["linked"] == ""
    assert payload["related"]["similar"]
    assert "related.linked" in _gap_sections(payload)


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
    assert {"section": "code.augment",
            "reason": "git-история недоступна: CalledProcessError"} in payload["gaps"]


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
