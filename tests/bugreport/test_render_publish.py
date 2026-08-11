"""Сборка issue, поиск дублей и фолбэк публикации (PRI-239)."""
import httpx
import pytest

from reviewer.bugreport.environment import collect_environment
from reviewer.bugreport.publish import IssueClient, find_duplicate, publish_report
from reviewer.bugreport.render import MARKER, prefilled_url, render_issue


def _report(summary="finish_task вернул поле не по контракту", severity="degraded",
            signature="abc123", **kwargs):
    return render_issue(
        summary=summary,
        expected=kwargs.get("expected", "три различимых исхода"),
        actual=kwargs.get("actual", "один bool"),
        steps=kwargs.get("steps", ["вызвать тул", "прочитать ответ"]),
        severity=severity,
        kind_reason="ответ MCP-тула не соответствует докстрингу",
        signature=signature,
        environment=collect_environment(client_fields={"orchestrator_model": "opus-5"}),
        details=kwargs.get("details", ""),
    )


def test_issue_has_every_required_section():
    body = _report().body
    for heading in ("## Что произошло", "## Ожидалось", "## Фактически",
                    "## Шаги воспроизведения", "## Классификация", "## Окружение"):
        assert heading in body
    assert MARKER in body


def test_title_carries_severity_and_is_capped():
    report = _report(summary="ы" * 300, severity="blocker")
    assert report.title.startswith("[blocker] ")
    assert len(report.title) <= 115


def test_empty_steps_render_explicitly_instead_of_silently_vanishing():
    assert "_(не указаны)_" in _report(steps=[]).body


def test_prefilled_url_carries_title_and_body():
    url = prefilled_url(_report())
    assert url.startswith("https://github.com/mimfort/rag_for_git/issues/new?")
    assert "title=" in url and "body=" in url


def test_duplicate_found_by_signature():
    issues = [{"number": 7, "title": "иное", "body": f"{MARKER}\n`abc123`",
               "html_url": "u"}]
    assert find_duplicate(issues, _report())["number"] == 7


def test_duplicate_found_by_title_overlap_for_hand_written_issues():
    issues = [{"number": 9, "title": "[degraded] finish_task вернул поле не по контракту",
               "body": "заведено руками", "html_url": "u"}]
    assert find_duplicate(issues, _report())["number"] == 9


def test_pull_requests_are_not_duplicates():
    client = IssueClient("t", client=httpx.Client(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json=[{"number": 1, "title": "x", "pull_request": {}}]))))
    assert client.list_open_issues() == []


def test_missing_token_falls_back_instead_of_raising():
    result = publish_report(_report(), token="")
    assert result.status == "fallback"
    assert result.fallback_url
    assert "токен" in result.reason


def test_api_failure_falls_back_and_never_breaks_the_session():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    client = IssueClient("t", client=httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    result = publish_report(_report(), token="t", client=client)
    assert result.status == "fallback"
    assert result.fallback_url


def test_successful_publication_returns_the_issue_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"html_url": "https://github.com/x/y/issues/5"})

    client = IssueClient("t", client=httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    result = publish_report(_report(), token="t", client=client)
    assert result.status == "published"
    assert result.url.endswith("/issues/5")


def test_duplicate_gets_a_comment_instead_of_a_second_issue():
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{
                "number": 12, "title": "t", "body": f"{MARKER}\nabc123",
                "html_url": "https://github.com/x/y/issues/12"}])
        posted.append(str(request.url))
        return httpx.Response(201, json={"html_url": "https://github.com/x/y/issues/12#c"})

    client = IssueClient("t", client=httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    result = publish_report(_report(), token="t", client=client)
    assert result.status == "commented"
    assert result.duplicate_url.endswith("/issues/12")
    assert any("comments" in url for url in posted)


@pytest.mark.parametrize("severity", ["blocker", "degraded", "contract"])
def test_every_severity_renders(severity):
    assert f"[{severity}]" in _report(severity=severity).title
