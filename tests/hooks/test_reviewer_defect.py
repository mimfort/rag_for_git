"""PostToolUse-хук автотриггера канала репорта багов (PRI-240).

Половина ценности набора — в тестах МОЛЧАНИЯ: шумящий хук выключат целиком, и канал
умрёт вместе с ним. Поэтому штатные сбои перечислены поимённо и закреплены.
"""
import importlib.util
import io
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK_PATH = ROOT / "plugin" / "hooks" / "reviewer_defect.py"


def _load():
    spec = importlib.util.spec_from_file_location("reviewer_defect", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


rd = _load()

REVIEWER_TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "/srv/app/reviewer/mcp/service.py", line 1157, in finish_task\n'
    '    link_status, link_warnings = self._backlink_pr(pr_url, key, url)\n'
    'KeyError: "task_link_status"'
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Свой tempdir на тест: дедуп не должен протекать между тестами."""
    monkeypatch.setattr(rd.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("REVIEW_BUG_REPORTS", raising=False)


def _run(response, tool="mcp__reviewer__finish_task", cwd="", session="s1", capsys=None):
    rd.run({"tool_name": tool, "tool_response": response, "cwd": cwd, "session_id": session})
    return capsys.readouterr().out if capsys else ""


# --- Молчание на штатных и чужих сбоях -------------------------------------------

FOREIGN_CASES = {
    "postgres не поднят": {"status": "error", "message":
                           "could not connect to server: Connection refused (Postgres)"},
    "neo4j не поднят": {"status": "error", "message":
                        "ServiceUnavailable: Unable to retrieve routing information"},
    "подсказка про docker": {"status": "error", "message":
                             "хранилища недоступны; выполните docker compose up -d"},
    "нет ключа Voyage": {"status": "error", "message": "VOYAGE_API_KEY не задан"},
    "нет токена VCS": {"status": "error", "message": "GITHUB_TOKEN не задан"},
    "лимит доски": {"status": "error", "category": "rate_limit", "message": "429"},
    "5xx доски": {"status": "error", "category": "transient", "message": "503 service unavailable"},
    "нет прав": {"status": "error", "category": "permission", "message": "403 forbidden"},
    "нет задачи": {"status": "error", "category": "not_found", "message": "404"},
    "не настроено": {"status": "error", "category": "configuration", "message": "board missing"},
    "сетевой таймаут": {"status": "error", "message": "read timed out"},
    "индекс не построен": {"status": "error", "message": "индекс не построен для ветки"},
    "индекс устарел": {"status": "error", "message": "индекс устарел на 12 коммитов"},
    "ветка вне allowlist": {"status": "skipped", "reason":
                            "целевая ветка не отслеживается (REVIEW_BRANCHES)"},
}


@pytest.mark.parametrize("case", sorted(FOREIGN_CASES), ids=sorted(FOREIGN_CASES))
def test_hook_stays_silent_on_routine_failures(case, capsys):
    assert _run(FOREIGN_CASES[case], capsys=capsys) == ""


def test_foreign_signal_overrides_a_reviewer_traceback():
    # Приоритет allowlist'а: трейсбек через наш код при упавшем Postgres — не наш баг.
    response = {"status": "error", "message": "Connection refused",
                "traceback": REVIEWER_TRACEBACK}
    assert rd.detect("mcp__reviewer__sync_board", response) is None


def test_retryable_marks_the_failure_as_foreign():
    assert rd.detect("mcp__reviewer__sync_board",
                     {"status": "error", "retryable": True,
                      "traceback": REVIEWER_TRACEBACK}) is None


def test_successful_response_is_silent(capsys):
    assert _run({"status": "ok", "done_set": True}, capsys=capsys) == ""


def test_non_reviewer_tool_is_ignored(capsys):
    assert _run(REVIEWER_TRACEBACK, tool="Bash", capsys=capsys) == ""


def test_third_party_traceback_is_not_ours():
    other = ('Traceback (most recent call last):\n'
             '  File "/srv/app/billing/rules.py", line 4, in apply\n'
             'ValueError: bad tier')
    assert rd.detect("mcp__reviewer__search_codebase", other) is None


# --- Срабатывание на дефектах инструмента ----------------------------------------

def test_reviewer_frame_in_traceback_is_detected():
    kind, value = rd.detect("mcp__reviewer__finish_task", REVIEWER_TRACEBACK)
    assert kind == "tool_exception"
    assert value


def test_traceback_survives_a_structured_response():
    # Регрессия: json.dumps экранировал кавычки и фрейм переставал совпадать.
    kind, _ = rd.detect("mcp__reviewer__finish_task",
                        {"status": "ok", "detail": REVIEWER_TRACEBACK})
    assert kind == "tool_exception"


def test_undocumented_status_is_a_contract_violation():
    kind, _ = rd.detect("mcp__reviewer__finish_task", {"status": "мимо контракта"})
    assert kind == "contract_violation"


def test_documented_statuses_do_not_fire():
    for status in ("ok", "error"):
        assert rd.detect("mcp__reviewer__finish_task", {"status": status}) is None
    assert rd.detect("mcp__reviewer__prepare_review", {"status": "skipped"}) is None


def test_unknown_tool_status_is_not_judged():
    # «Я не знаю контракта» и «контракт нарушен» — разные вещи.
    assert rd.detect("mcp__reviewer__get_impact", {"status": "что-то своё"}) is None


def test_both_tool_name_prefixes_are_recognized():
    for prefix in rd.MCP_TOOL_PREFIXES:
        assert rd.detect(prefix + "finish_task", REVIEWER_TRACEBACK) is not None


def test_nudge_carries_only_the_shape_of_the_failure(capsys):
    out = _run(REVIEWER_TRACEBACK, capsys=capsys)
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "report-bug" in context
    assert "tool_exception" in context
    for leaked in ("/srv/app", "reviewer/mcp/service.py", "task_link_status", "KeyError"):
        assert leaked not in context, leaked


def test_nudge_never_asks_to_publish_autonomously(capsys):
    context = json.loads(_run(REVIEWER_TRACEBACK, capsys=capsys))[
        "hookSpecificOutput"]["additionalContext"]
    assert "явного согласия" in context
    assert "ничего не публикуешь сам" in context


# --- Порог шума, выключатели, fail-open ------------------------------------------

def test_same_symptom_nudges_only_once_per_session(capsys):
    assert _run(REVIEWER_TRACEBACK, capsys=capsys) != ""
    assert _run(REVIEWER_TRACEBACK, capsys=capsys) == ""


def test_a_different_session_gets_its_own_reminder(capsys):
    assert _run(REVIEWER_TRACEBACK, session="a", capsys=capsys) != ""
    assert _run(REVIEWER_TRACEBACK, session="b", capsys=capsys) != ""


def test_deploy_switch_silences_the_hook(monkeypatch, capsys):
    monkeypatch.setenv("REVIEW_BUG_REPORTS", "false")
    assert _run(REVIEWER_TRACEBACK, capsys=capsys) == ""


def test_repo_switch_silences_the_hook(tmp_path, capsys):
    (tmp_path / ".review.yml").write_text("bug_reports: false\n", encoding="utf-8")
    assert _run(REVIEWER_TRACEBACK, cwd=str(tmp_path), capsys=capsys) == ""


def test_repo_switch_on_keeps_the_hook_alive(tmp_path, capsys):
    (tmp_path / ".review.yml").write_text("bug_reports: true\n", encoding="utf-8")
    assert _run(REVIEWER_TRACEBACK, cwd=str(tmp_path), capsys=capsys) != ""


def test_unreadable_config_does_not_silence_the_hook(capsys):
    # Неспособность хука разобрать конфиг — не причина терять сигнал.
    assert _run(REVIEWER_TRACEBACK, cwd="/nope/missing", capsys=capsys) != ""


def test_hook_is_fail_open_on_broken_payload():
    assert rd.run({"tool_name": None, "tool_response": object()}) == 0
    assert rd.run({}) == 0


def test_main_survives_invalid_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert rd.main() == 0


def test_kinds_match_the_server_side_triage_vocabulary():
    """Хук и сервер обязаны говорить на одном языке классов симптомов."""
    from reviewer.bugreport.triage import OUR_KINDS

    for tool in rd.DOCUMENTED_STATUSES:
        assert isinstance(tool, str)
    assert "tool_exception" in OUR_KINDS
    assert "contract_violation" in OUR_KINDS


def test_foreign_categories_match_the_board_error_vocabulary():
    from reviewer.tasks.boards.errors import _CATEGORIES

    assert rd.FOREIGN_ERROR_CATEGORIES == _CATEGORIES
