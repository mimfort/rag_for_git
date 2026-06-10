import json
import pytest
import httpx
from reviewer.vcs.github import GitHubProvider, _RetryTransport
from reviewer.vcs.base import InlineComment


def make_provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://api.github.com")
    return GitHubProvider("o", "r", token="t", client=client)


def make_retry_provider(handler, *, attempts=3, backoff_base=1.0, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    base_transport = httpx.MockTransport(handler)
    retry_transport = _RetryTransport(
        base_transport,
        attempts=attempts,
        backoff_base=backoff_base,
        _sleep=sleeps.append,
    )
    client = httpx.Client(transport=retry_transport,
                          base_url="https://api.github.com")
    return GitHubProvider("o", "r", token="t", client=client), sleeps


def test_get_pull_request_draft_true():
    def handler(req):
        if req.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json={
                "base": {"sha": "aaa", "ref": "main"},
                "head": {"sha": "bbb"},
                "title": "My PR",
                "body": "desc",
                "draft": True,
            })
        return httpx.Response(404)
    p = make_provider(handler)
    pr = p.get_pull_request(7)
    assert pr.draft is True


def test_get_pull_request_draft_false():
    def handler(req):
        if req.url.path.endswith("/pulls/8"):
            return httpx.Response(200, json={
                "base": {"sha": "ccc", "ref": "main"},
                "head": {"sha": "ddd"},
                "title": "Ready PR",
                "body": "",
                "draft": False,
            })
        return httpx.Response(404)
    p = make_provider(handler)
    pr = p.get_pull_request(8)
    assert pr.draft is False


def test_compare_files_returns_changed_files():
    def handler(req):
        if "/compare/" in req.url.path:
            return httpx.Response(200, json={
                "files": [
                    {"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"},
                    {"filename": "b.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+new"},
                    {"filename": "c.py", "status": "removed"},
                ]
            })
        return httpx.Response(404)
    p = make_provider(handler)
    files = p.compare_files("aaa111", "bbb222")
    assert len(files) == 3
    assert files[0].path == "a.py"
    assert files[0].status == "modified"
    assert files[0].patch == "@@ -1 +1 @@\n-old\n+new"
    assert files[1].path == "b.py"
    assert files[1].status == "added"
    assert files[2].path == "c.py"
    assert files[2].status == "removed"
    assert files[2].patch is None


def test_compare_files_404_raises():
    def handler(req):
        return httpx.Response(404)
    p = make_provider(handler)
    with pytest.raises(httpx.HTTPStatusError):
        p.compare_files("dead", "beef")


def test_list_existing_fingerprints_parses_markers():
    def handler(req):
        if req.url.path.endswith("/comments"):
            body = [{"body": "issue\n<!-- ai-review:abc123 -->"}]
            return httpx.Response(200, json=body)
        return httpx.Response(404)
    p = make_provider(handler)
    assert p.list_existing_fingerprints(5) == {"abc123"}


def test_publish_review_posts_review_payload():
    captured = {}

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/reviews"):
            captured.update(json.loads(req.content))
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404)
    p = make_provider(handler)
    p.publish_review(5, "deadbeef", "Сводка",
                     [InlineComment("a.py", 10, "RIGHT", "body\n<!-- ai-review:fp1 -->")])
    assert captured["event"] == "COMMENT"
    assert captured["commit_id"] == "deadbeef"
    assert captured["comments"][0] == {"path": "a.py", "line": 10,
                                       "side": "RIGHT", "body": "body\n<!-- ai-review:fp1 -->"}


def test_retry_429_eventually_succeeds():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json={
            "base": {"sha": "aaa", "ref": "main"},
            "head": {"sha": "bbb"},
            "title": "PR",
            "body": "",
            "draft": False,
        })

    p, sleeps = make_retry_provider(handler, attempts=3, backoff_base=1.0)
    pr = p.get_pull_request(1)
    assert pr.title == "PR"
    assert len(calls) == 3
    assert sleeps == [1.0, 1.0]


def test_retry_502_exhausted_raises():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(502)

    p, sleeps = make_retry_provider(handler, attempts=3, backoff_base=2.0)
    with pytest.raises(httpx.HTTPStatusError):
        p.get_pull_request(1)
    assert len(calls) == 3
    assert sleeps == [2.0, 4.0]


def test_retry_respects_retry_after_header():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(503, headers={"retry-after": "5"})
        return httpx.Response(200, json={
            "base": {"sha": "aaa", "ref": "main"},
            "head": {"sha": "bbb"},
            "title": "PR",
            "body": "",
            "draft": False,
        })

    p, sleeps = make_retry_provider(handler, attempts=3, backoff_base=1.0)
    pr = p.get_pull_request(1)
    assert pr.title == "PR"
    assert len(calls) == 2
    assert sleeps == [5.0]


@pytest.mark.parametrize("status", [401, 403, 422])
def test_no_retry_on_401_403_422(status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status)

    p, sleeps = make_retry_provider(handler, attempts=3, backoff_base=1.0)
    with pytest.raises(httpx.HTTPStatusError):
        p.get_pull_request(1)
    assert len(calls) == 1
    assert sleeps == []


def test_retry_504_then_success():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(504)
        return httpx.Response(200, json={
            "base": {"sha": "aaa", "ref": "main"},
            "head": {"sha": "bbb"},
            "title": "PR",
            "body": "",
            "draft": False,
        })

    p, sleeps = make_retry_provider(handler, attempts=3, backoff_base=1.0)
    pr = p.get_pull_request(1)
    assert pr.title == "PR"
    assert len(calls) == 2
    assert sleeps == [1.0]
