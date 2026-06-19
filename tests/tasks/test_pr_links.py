"""Unit-тесты парсера GitHub-PR-URL для авто-линковки задач."""
from reviewer.tasks.pr_links import extract_pr_refs


def test_extract_single_pr_url():
    refs = extract_pr_refs("см. https://github.com/mimfort/rag_for_git/pull/20 — детали")
    assert len(refs) == 1
    r = refs[0]
    assert r.repo == "mimfort/rag_for_git"
    assert r.number == 20
    assert r.url == "https://github.com/mimfort/rag_for_git/pull/20"
    assert r.sha == ""


def test_extract_multiple_and_dedup():
    text = (
        "PR https://github.com/o/r/pull/7 и снова https://github.com/o/r/pull/7 "
        "плюс http://github.com/o/r/pull/8"
    )
    refs = extract_pr_refs(text)
    assert [(r.repo, r.number) for r in refs] == [("o/r", 7), ("o/r", 8)]


def test_ignores_non_pr_github_urls():
    text = (
        "issue https://github.com/o/r/issues/5 файл "
        "https://github.com/o/r/blob/main/x.py"
    )
    assert extract_pr_refs(text) == []


def test_empty_and_none_safe():
    assert extract_pr_refs("") == []
    assert extract_pr_refs("без ссылок") == []
