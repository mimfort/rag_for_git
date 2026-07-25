"""Юниты обратного линка задачи в тело PR: разбор ссылки + идемпотентная вставка."""
from reviewer.tasks.pr_backlink import MARKER, apply_backlink, parse_pr_url

URL = "https://ru.yougile.com/team/686c049c8af8/#PRI-216"


def test_parse_github_pr_url():
    t = parse_pr_url("https://github.com/mimfort/rag_for_git/pull/128")
    assert (t.platform, t.owner, t.repo, t.number) == (
        "github", "mimfort", "rag_for_git", 128)
    # GitHubProvider ходит в api.github.com — хост из ссылки не нужен
    assert t.base_url == ""


def test_parse_gitlab_mr_url_with_nested_groups():
    t = parse_pr_url("https://gitlab.example.ru/team/sub/svc/-/merge_requests/42")
    assert (t.platform, t.owner, t.repo, t.number) == ("gitlab", "team/sub", "svc", 42)
    # self-hosted: базовый URL API берётся из самой ссылки
    assert t.base_url == "https://gitlab.example.ru"


def test_parse_gitlab_mr_url_flat_namespace():
    t = parse_pr_url("https://gitlab.com/group/proj/-/merge_requests/3")
    assert (t.owner, t.repo, t.number) == ("group", "proj", 3)


def test_parse_ignores_url_tails():
    for tail in ("/files", "?tab=files", "#note_1", "/"):
        t = parse_pr_url(f"https://github.com/o/r/pull/7{tail}")
        assert t is not None and t.number == 7, tail


def test_parse_rejects_unrecognized():
    for bad in ("", "url", "https://github.com/o/r/pulls",
                "https://github.com/o/r/issues/7", "not a url at all"):
        assert parse_pr_url(bad) is None, bad


def test_apply_backlink_prepends_line_and_marker():
    out = apply_backlink("## Задача\n\nтекст", "PRI-216", URL)
    assert out == f"Задача: [PRI-216]({URL})\n{MARKER}\n\n## Задача\n\nтекст"


def test_apply_backlink_on_empty_body():
    assert apply_backlink("", "PRI-216", URL) == f"Задача: [PRI-216]({URL})\n{MARKER}"


def test_apply_backlink_noop_when_marker_present():
    body = f"Задача: [PRI-216]({URL})\n{MARKER}\n\nтекст"
    assert apply_backlink(body, "PRI-216", URL) is None


def test_apply_backlink_noop_when_url_already_in_body():
    # ручная ссылка без маркера уважается — дубля не будет
    assert apply_backlink(f"## Задача\n\n[PRI-216]({URL}) — описание", "PRI-216", URL) is None
