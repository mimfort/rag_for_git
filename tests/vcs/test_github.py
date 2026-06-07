import json, httpx
from reviewer.vcs.github import GitHubProvider
from reviewer.vcs.base import InlineComment

def make_provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://api.github.com")
    return GitHubProvider("o", "r", token="t", client=client)

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
