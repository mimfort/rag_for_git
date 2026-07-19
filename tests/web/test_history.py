"""Тесты ReviewHistory.

Unit fail-soft мокает `_connect`; integration проверяет запись/чтение
в изолированном профиле:
`docker compose --profile test up -d --wait paradedb-test`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from reviewer.config.settings import Settings
from reviewer.web.history import ReviewHistory


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

def _sample_run() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "repo": "owner/repo",
        "pr_number": 42,
        "base_sha": "abc1234",
        "head_sha": "def5678",
        "model": "anthropic/claude-sonnet-4.5",
        "model_verify": None,
        "dry_run": False,
        "started_at": now,
        "finished_at": now,
        "duration_ms": 1500,
        "status": "ok",
        "files_reviewed": 3,
        "files_skipped": 0,
        "files_failed": 0,
        "findings_analyzed": 5,
        "findings_kept": 3,
        "verify_rejected": 2,
        "comments_inline": 2,
        "comments_summary": 1,
        "usage": {"analyze": {"calls": 3, "input_tokens": 1000, "output_tokens": 200,
                               "cache_read_tokens": 0, "cost": 0.01}},
        "total_cost": 0.01,
        "error_text": None,
    }


def _sample_findings() -> list[dict]:
    return [
        {
            "file": "reviewer/app.py",
            "line": 10,
            "category": "correctness",
            "severity": "high",
            "confidence": 0.9,
            "is_real": True,
            "published": True,
            "inline": True,
            "fingerprint": "abc123",
            "message": "Потенциальный NullPointerException",
        },
        {
            "file": "reviewer/app.py",
            "line": None,
            "category": "style",
            "severity": "low",
            "confidence": 0.6,
            "is_real": True,
            "published": True,
            "inline": False,
            "fingerprint": "def456",
            "message": "Нарушение стиля кода",
        },
    ]


# ---------------------------------------------------------------------------
# Unit: fail-soft при недоступной БД
# ---------------------------------------------------------------------------

def test_record_run_fail_soft_on_bad_dsn():
    """record_run() возвращает None при ошибке подключения — не бросает исключение."""
    history = ReviewHistory("postgresql://bad:bad@localhost:1/nonexistent")

    with patch.object(history, "_connect", side_effect=OSError("database unavailable")):
        result = history.record_run(_sample_run(), _sample_findings())

    assert result is None


def test_record_run_fail_soft_on_exception():
    """record_run() возвращает None при любом исключении в соединении."""
    history = ReviewHistory("postgresql://reviewer:reviewer@localhost:5433/reviewer")

    with patch("reviewer.web.history.ConnectionPool") as mock_pool:
        mock_pool.side_effect = RuntimeError("тестовая ошибка")
        result = history.record_run(_sample_run(), _sample_findings())

    assert result is None


def test_get_trace_fail_soft_on_bad_dsn():
    """get_trace() возвращает [] при недоступной БД — не бросает исключение."""
    history = ReviewHistory("postgresql://bad:bad@localhost:1/nonexistent")

    with patch.object(history, "_connect", side_effect=OSError("database unavailable")):
        result = history.get_trace(1)

    assert result == []


def test_get_trace_fail_soft_on_exception():
    """get_trace() возвращает [] при любом исключении — fail-soft."""
    history = ReviewHistory("postgresql://reviewer:reviewer@localhost:5433/reviewer")

    with patch("reviewer.web.history.ConnectionPool") as mock_pool:
        mock_pool.side_effect = RuntimeError("тестовая ошибка")
        result = history.get_trace(1)

    assert result == []


# ---------------------------------------------------------------------------
# Integration: запись и чтение прогона
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_record_and_get_run():
    """Записать прогон с находками и прочитать его обратно."""
    from reviewer.config.settings import Settings
    pg_dsn = Settings().pg_dsn

    history = ReviewHistory(pg_dsn)
    history.init_schema()

    run = _sample_run()
    findings = _sample_findings()
    run_id = history.record_run(run, findings)
    assert run_id is not None
    assert isinstance(run_id, int)

    result = history.get_run(run_id)
    assert result is not None
    assert result["repo"] == "owner/repo"
    assert result["pr_number"] == 42
    assert result["status"] == "ok"
    assert result["files_reviewed"] == 3
    assert result["findings_kept"] == 3
    assert len(result["findings"]) == 2

    f0 = result["findings"][0]
    assert f0["file"] == "reviewer/app.py"
    assert f0["category"] == "correctness"
    assert f0["severity"] == "high"
    assert f0["inline"] is True


@pytest.mark.integration
def test_get_run_returns_none_for_unknown_id():
    """get_run() возвращает None для несуществующего id."""
    from reviewer.config.settings import Settings
    pg_dsn = Settings().pg_dsn

    history = ReviewHistory(pg_dsn)
    history.init_schema()

    result = history.get_run(999_999_999)
    assert result is None


@pytest.mark.integration
def test_list_runs_filters():
    """list_runs() фильтрует по repo и status."""
    from reviewer.config.settings import Settings
    pg_dsn = Settings().pg_dsn

    history = ReviewHistory(pg_dsn)
    history.init_schema()

    run1 = {**_sample_run(), "repo": "owner/repo-a", "status": "ok"}
    run2 = {**_sample_run(), "repo": "owner/repo-b", "status": "error"}
    history.record_run(run1, [])
    history.record_run(run2, [])

    by_repo = history.list_runs(repo="owner/repo-a")
    assert all(r["repo"] == "owner/repo-a" for r in by_repo)

    by_status = history.list_runs(status="error")
    assert all(r["status"] == "error" for r in by_status)


@pytest.mark.integration
def test_stats_returns_expected_shape():
    """stats() возвращает словарь с нужными ключами."""
    from reviewer.config.settings import Settings
    pg_dsn = Settings().pg_dsn

    history = ReviewHistory(pg_dsn)
    history.init_schema()
    history.record_run(_sample_run(), _sample_findings())

    data = history.stats(days=30)
    assert "total_runs" in data
    assert "total_cost" in data
    assert "avg_cost_per_run" in data
    assert "total_findings" in data
    assert "verify_reject_rate" in data
    assert "cost_over_time" in data
    assert "runs_over_time" in data
    assert "findings_by_category" in data
    assert "findings_by_severity" in data

    assert data["total_runs"] >= 1
    assert isinstance(data["cost_over_time"], list)
    assert isinstance(data["findings_by_category"], list)


# ---------------------------------------------------------------------------
# Integration: запись и чтение трейса (review_steps)
# ---------------------------------------------------------------------------

def _sample_steps() -> list[dict]:
    """Список шагов трейса в формате TraceLog.snapshot()."""
    return [
        {
            "seq": 1,
            "stage": "analyze",
            "unit": "reviewer/app.py",
            "kind": "prompt",
            "name": None,
            "text": "Текст промпта",
            "tool_calls": None,
            "tokens": 0,
            "cost": 0.0,
        },
        {
            "seq": 2,
            "stage": "analyze",
            "unit": "reviewer/app.py",
            "kind": "llm_call",
            "name": None,
            "text": "Рассуждение модели",
            "tool_calls": [{"name": "search_code", "args": {"query": "def connect"}}],
            "tokens": 250,
            "cost": 0.005,
        },
        {
            "seq": 3,
            "stage": "analyze",
            "unit": "reviewer/app.py",
            "kind": "tool_call",
            "name": "search_code",
            "text": "Результат поиска",
            "tool_calls": [{"name": "search_code", "args": {"query": "def connect"}}],
            "tokens": 0,
            "cost": 0.0,
        },
    ]


@pytest.mark.integration
def test_record_run_with_steps_and_get_trace():
    """Записать прогон с шагами трейса и прочитать их через get_trace()."""
    from reviewer.config.settings import Settings
    pg_dsn = Settings().pg_dsn

    history = ReviewHistory(pg_dsn)
    history.init_schema()

    run_id = history.record_run(_sample_run(), _sample_findings(), steps=_sample_steps())
    assert run_id is not None

    trace = history.get_trace(run_id)
    assert isinstance(trace, list)
    assert len(trace) == 3

    # Шаги должны быть упорядочены по seq
    seqs = [s["seq"] for s in trace]
    assert seqs == sorted(seqs)

    # Проверяем поля первого шага
    s0 = trace[0]
    assert s0["stage"] == "analyze"
    assert s0["unit"] == "reviewer/app.py"
    assert s0["kind"] == "prompt"
    assert s0["text"] == "Текст промпта"
    assert s0["tokens"] == 0

    # Проверяем шаг с llm_call — tool_calls должен вернуться как список
    s1 = trace[1]
    assert s1["kind"] == "llm_call"
    assert s1["tool_calls"] is not None
    assert isinstance(s1["tool_calls"], list)
    assert s1["tool_calls"][0]["name"] == "search_code"


@pytest.mark.integration
def test_get_trace_empty_for_run_without_steps():
    """get_trace() возвращает [] для прогона без шагов."""
    from reviewer.config.settings import Settings
    pg_dsn = Settings().pg_dsn

    history = ReviewHistory(pg_dsn)
    history.init_schema()

    run_id = history.record_run(_sample_run(), [], steps=None)
    assert run_id is not None

    trace = history.get_trace(run_id)
    assert trace == []


@pytest.mark.integration
def test_get_trace_unknown_run_id():
    """get_trace() возвращает [] для несуществующего run_id."""
    from reviewer.config.settings import Settings
    pg_dsn = Settings().pg_dsn

    history = ReviewHistory(pg_dsn)
    history.init_schema()

    trace = history.get_trace(999_999_998)
    assert trace == []


@pytest.mark.integration
def test_days_since_last_run():
    pg_dsn = Settings().pg_dsn
    history = ReviewHistory(pg_dsn)
    history.init_schema()
    run = _sample_run()
    history.record_run(run, _sample_findings())
    assert history.days_since_last_run(run["repo"]) == 0
    assert history.days_since_last_run("nonexistent/repo") is None
