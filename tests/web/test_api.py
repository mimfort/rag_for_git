"""Тесты API-роутера reviewer.web.api.

Не требуют реального Postgres — используют фейковый ReviewHistory.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reviewer.web.api import make_router


# ---------------------------------------------------------------------------
# Фейковый стор
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc).isoformat()

_FAKE_RUN = {
    "id": 1,
    "created_at": _NOW,
    "repo": "owner/repo",
    "pr_number": 42,
    "base_sha": "abc1234",
    "head_sha": "def5678",
    "model": "anthropic/claude-sonnet-4.5",
    "model_verify": None,
    "dry_run": False,
    "started_at": _NOW,
    "finished_at": _NOW,
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

_FAKE_RUN_WITH_FINDINGS = {
    **_FAKE_RUN,
    "findings": [
        {
            "id": 1,
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
    ],
}

_FAKE_STATS = {
    "total_runs": 10,
    "total_cost": 0.5,
    "avg_cost_per_run": 0.05,
    "total_findings": 30,
    "verify_reject_rate": 0.2,
    "cost_over_time": [{"date": "2026-06-01", "cost": 0.1}],
    "runs_over_time": [{"date": "2026-06-01", "count": 3}],
    "findings_by_category": [{"category": "correctness", "count": 15}],
    "findings_by_severity": [{"severity": "high", "count": 10}],
}


class FakeHistory:
    """Фейковый ReviewHistory для тестов без БД."""

    def list_runs(
        self,
        repo=None,
        status=None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        runs = [_FAKE_RUN]
        if repo is not None:
            runs = [r for r in runs if r["repo"] == repo]
        if status is not None:
            runs = [r for r in runs if r["status"] == status]
        return runs[offset: offset + limit]

    def get_run(self, run_id: int) -> dict | None:
        if run_id == 1:
            return _FAKE_RUN_WITH_FINDINGS
        return None

    def stats(self, days: int = 30) -> dict:
        return _FAKE_STATS


# ---------------------------------------------------------------------------
# Фикстура: TestClient с фейковым стором
# ---------------------------------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    router = make_router(FakeHistory())
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Тесты /api/runs
# ---------------------------------------------------------------------------

def test_list_runs_returns_200(client):
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert "runs" in data
    assert isinstance(data["runs"], list)
    assert "limit" in data
    assert "offset" in data


def test_list_runs_contains_expected_fields(client):
    resp = client.get("/api/runs")
    runs = resp.json()["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["repo"] == "owner/repo"
    assert run["pr_number"] == 42
    assert run["status"] == "ok"
    assert run["total_cost"] == 0.01


def test_list_runs_filter_by_repo_found(client):
    resp = client.get("/api/runs", params={"repo": "owner/repo"})
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1


def test_list_runs_filter_by_repo_not_found(client):
    resp = client.get("/api/runs", params={"repo": "other/repo"})
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 0


def test_list_runs_filter_by_status(client):
    resp = client.get("/api/runs", params={"status": "ok"})
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert all(r["status"] == "ok" for r in runs)


def test_list_runs_pagination(client):
    resp = client.get("/api/runs", params={"limit": 10, "offset": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 10
    assert data["offset"] == 0


# ---------------------------------------------------------------------------
# Тесты /api/runs/{id}
# ---------------------------------------------------------------------------

def test_get_run_200(client):
    resp = client.get("/api/runs/1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert data["repo"] == "owner/repo"
    assert "findings" in data
    assert isinstance(data["findings"], list)
    assert len(data["findings"]) == 1


def test_get_run_findings_fields(client):
    resp = client.get("/api/runs/1")
    f = resp.json()["findings"][0]
    assert "file" in f
    assert "line" in f
    assert "category" in f
    assert "severity" in f
    assert "confidence" in f
    assert "is_real" in f
    assert "published" in f
    assert "inline" in f
    assert "fingerprint" in f
    assert "message" in f


def test_get_run_404_for_unknown_id(client):
    resp = client.get("/api/runs/999")
    assert resp.status_code == 404
    assert "не найден" in resp.json()["detail"].lower() or "404" in str(resp.status_code)


# ---------------------------------------------------------------------------
# Тесты /api/stats
# ---------------------------------------------------------------------------

def test_get_stats_200(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_runs"] == 10
    assert data["total_cost"] == 0.5
    assert data["avg_cost_per_run"] == 0.05
    assert data["total_findings"] == 30
    assert data["verify_reject_rate"] == 0.2


def test_get_stats_time_series(client):
    resp = client.get("/api/stats")
    data = resp.json()
    assert isinstance(data["cost_over_time"], list)
    assert isinstance(data["runs_over_time"], list)
    cost_point = data["cost_over_time"][0]
    assert "date" in cost_point
    assert "cost" in cost_point
    runs_point = data["runs_over_time"][0]
    assert "date" in runs_point
    assert "count" in runs_point


def test_get_stats_breakdown(client):
    resp = client.get("/api/stats")
    data = resp.json()
    assert isinstance(data["findings_by_category"], list)
    assert isinstance(data["findings_by_severity"], list)
    cat = data["findings_by_category"][0]
    assert "category" in cat and "count" in cat
    sev = data["findings_by_severity"][0]
    assert "severity" in sev and "count" in sev


def test_get_stats_custom_days(client):
    resp = client.get("/api/stats", params={"days": 7})
    assert resp.status_code == 200


def test_get_stats_days_validation(client):
    """days должен быть в диапазоне 1..365."""
    resp = client.get("/api/stats", params={"days": 0})
    assert resp.status_code == 422

    resp = client.get("/api/stats", params={"days": 366})
    assert resp.status_code == 422
