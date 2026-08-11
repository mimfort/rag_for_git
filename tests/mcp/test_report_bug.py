"""Server-side канал репорта багов: апрув, выключатель, дедуп, headless (PRI-239).

Главный инвариант проверяется здесь, а не в guard-тестах промпта: скилл — это текст,
а гарантию «без явного апрува ничего не публикуется» даёт сервер.
"""
from unittest.mock import MagicMock

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _settings(**overrides) -> Settings:
    values = {
        "github_token": "ghp_" + "a" * 36,
        "voyage_api_key": "pa-" + "b" * 30,
        "review_branches": "dev,main",
        "default_repo": "acmecorp/billing-api",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


class _Svc(MCPReviewService):
    """Сервис без Postgres/Neo4j: канал репорта не должен от них зависеть."""

    def __init__(self, settings=None, policy_bug_reports=True):
        super().__init__(settings or _settings(), MagicMock())
        self._policy_bug_reports = policy_bug_reports
        self.published: list = []

    def _resolve_policy(self, repo, branch):
        policy = MagicMock()
        policy.bug_reports = self._policy_bug_reports
        return policy, MagicMock()


def _preview(svc: _Svc, **kwargs) -> dict:
    payload = {
        "kind": "contract_violation",
        "summary": "тул вернул поле не по контракту",
        "expected": "три исхода",
        "actual": "один bool",
        "severity": "degraded",
        "tool": "finish_task",
    }
    payload.update(kwargs)
    return svc.report_bug(**payload)


def test_preview_returns_the_full_issue_text_and_publishes_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr("reviewer.bugreport.publish.publish_report",
                        lambda *a, **k: calls.append(a) or None)
    out = _preview(_Svc())
    assert out["status"] == "preview"
    assert "## Что произошло" in out["issue_text"]
    assert calls == []


def test_preview_warns_that_the_username_becomes_public():
    out = _preview(_Svc())
    assert "ник" in out["identity_notice"]


def test_foreign_problem_is_not_reported():
    out = _preview(_Svc(), kind="environment", summary="Postgres не поднят")
    assert out["status"] == "not_reported"
    assert "issue_text" not in out


def test_channel_disabled_by_deploy_switch():
    out = _preview(_Svc(_settings(review_bug_reports=False)))
    assert out["status"] == "disabled"
    assert "REVIEW_BUG_REPORTS" in out["reason"]


def test_channel_disabled_by_repo_policy():
    out = _preview(_Svc(policy_bug_reports=False), repo="acmecorp/billing-api", branch="dev")
    assert out["status"] == "disabled"
    assert ".review.yml" in out["reason"]


def test_same_symptom_is_offered_only_once_per_session():
    svc = _Svc()
    assert _preview(svc)["status"] == "preview"
    assert _preview(svc)["status"] == "suppressed"


def test_a_different_symptom_is_still_offered():
    svc = _Svc()
    _preview(svc)
    assert _preview(svc, tool="sync_board")["status"] == "preview"


def test_decline_is_remembered_and_suppresses_later_offers():
    svc = _Svc()
    assert _preview(svc, decline=True)["status"] == "declined"
    assert _preview(svc)["status"] == "suppressed"


def test_headless_run_never_publishes_even_when_confirmed(monkeypatch):
    calls: list = []
    monkeypatch.setattr("reviewer.bugreport.publish.publish_report",
                        lambda *a, **k: calls.append(a))
    out = _preview(_Svc(), confirmed=True, non_interactive=True)
    assert out["status"] == "fallback"
    assert out["fallback_url"].startswith("https://github.com/mimfort/rag_for_git/issues/new")
    assert calls == []


def test_confirmed_interactive_call_publishes(monkeypatch):
    class _Result:
        def as_dict(self):
            return {"status": "published", "issue_url": "https://github.com/x/y/issues/1"}
        status = "published"

    monkeypatch.setattr("reviewer.bugreport.publish.publish_report",
                        lambda *a, **k: _Result())
    out = _preview(_Svc(), confirmed=True)
    assert out["status"] == "published"
    assert out["issue_url"].endswith("/issues/1")


def test_contract_level_report_is_deferred_to_the_end_of_the_session():
    assert _preview(_Svc(), severity="contract")["defer"] is True
    assert _preview(_Svc(), severity="blocker")["defer"] is False


def test_installation_literals_never_reach_the_issue_text():
    svc = _Svc()
    out = _preview(
        svc,
        repo="acmecorp/billing-api",
        branch="dev",
        summary="в /Users/kate/acmecorp/billing-api на ветке dev упал BILL-77",
        details="токен ghp_" + "a" * 36,
    )
    text = out["issue_text"]
    for leaked in ("acmecorp", "billing-api", "kate", "BILL-77", "ghp_" + "a" * 36):
        assert leaked not in text, leaked


def test_environment_trimming_does_not_block_the_report():
    out = _preview(_Svc(), environment_include=[])
    assert out["status"] == "preview"
    assert out["environment_keys"] == []
    assert "исключён пользователем" in out["issue_text"]


def test_environment_carries_the_orchestrator_model():
    out = _preview(_Svc(), client_environment={"orchestrator_model": "opus-5",
                                               "mode": "subagent"})
    assert "orchestrator_model" in out["environment_keys"]
    assert "opus-5" in out["issue_text"]


def test_model_cannot_inject_arbitrary_environment_keys():
    out = _preview(_Svc(), client_environment={"repo_path": "/srv/acmecorp/billing-api"})
    assert "repo_path" not in out["environment_keys"]
    assert "acmecorp" not in out["issue_text"]


@pytest.mark.parametrize("kind", ["user_code", "permission", "external_service"])
def test_channel_stays_silent_on_classic_foreign_failures(kind):
    assert _preview(_Svc(), kind=kind)["status"] == "not_reported"
