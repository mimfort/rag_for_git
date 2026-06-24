import json
import httpx
from reviewer.vcs.gitlab import GitLabProvider
from reviewer.vcs.base import InlineComment


def make_provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://gitlab.com/api/v4")
    return GitLabProvider("o", "r", token="t", client=client)


def test_get_pull_request_maps_diff_refs_and_branches():
    def handler(req):
        if req.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "aaa", "head_sha": "bbb", "start_sha": "aaa"},
                "target_branch": "main",
                "source_branch": "feature/x",
                "title": "My MR",
                "description": "desc",
                "draft": True,
            })
        return httpx.Response(404)
    p = make_provider(handler)
    pr = p.get_pull_request(7)
    assert pr.base_sha == "aaa"
    assert pr.head_sha == "bbb"
    assert pr.base_ref == "main"
    assert pr.head_ref == "feature/x"
    assert pr.title == "My MR"
    assert pr.body == "desc"
    assert pr.draft is True


def test_get_changed_files_maps_status():
    def handler(req):
        if req.url.path.endswith("/merge_requests/3/changes"):
            return httpx.Response(200, json={"changes": [
                {"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@",
                 "new_file": False, "deleted_file": False, "renamed_file": False},
                {"old_path": "b.py", "new_path": "b.py", "diff": "@@ +1 @@",
                 "new_file": True, "deleted_file": False, "renamed_file": False},
                {"old_path": "c.py", "new_path": "c.py", "diff": "",
                 "new_file": False, "deleted_file": True, "renamed_file": False},
            ]})
        return httpx.Response(404)
    p = make_provider(handler)
    files = p.get_changed_files(3)
    assert (files[0].path, files[0].status) == ("a.py", "modified")
    assert (files[1].path, files[1].status) == ("b.py", "added")
    assert (files[2].path, files[2].status) == ("c.py", "removed")


def test_get_file_at_ref_decodes_base64():
    import base64
    def handler(req):
        if "/repository/files/" in req.url.path:
            return httpx.Response(200, json={
                "content": base64.b64encode(b"hello").decode(), "encoding": "base64"})
        return httpx.Response(404)
    p = make_provider(handler)
    assert p.get_file_at_ref("dir/f.py", "main") == "hello"


def test_get_file_at_ref_404_returns_none():
    p = make_provider(lambda req: httpx.Response(404))
    assert p.get_file_at_ref("missing.py", "main") is None


def test_list_existing_fingerprints_parses_markers():
    def handler(req):
        if req.url.path.endswith("/notes"):
            return httpx.Response(200, json=[{"body": "issue\n<!-- ai-review:abc123 -->"}])
        return httpx.Response(404)
    p = make_provider(handler)
    assert p.list_existing_fingerprints(5) == {"abc123"}


def test_compare_files_maps_diffs():
    def handler(req):
        if req.url.path.endswith("/repository/compare"):
            return httpx.Response(200, json={"diffs": [
                {"old_path": "a.py", "new_path": "a.py", "diff": "@@ @@",
                 "new_file": False, "deleted_file": False, "renamed_file": False},
            ]})
        return httpx.Response(404)
    p = make_provider(handler)
    files = p.compare_files("base", "head")
    assert files[0].path == "a.py"
    assert files[0].status == "modified"


def test_publish_review_posts_summary_note_and_discussion():
    posts = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/merge_requests/5"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "b1", "head_sha": "h1", "start_sha": "s1"},
                "target_branch": "main", "source_branch": "x",
                "title": "T", "description": "", "draft": False})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            posts.append(("note", json.loads(req.content)))
            return httpx.Response(201, json={"id": 1})
        if req.method == "POST" and req.url.path.endswith("/discussions"):
            posts.append(("discussion", json.loads(req.content)))
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    p = make_provider(handler)
    p.publish_review(5, "h1", "Сводка",
                     [InlineComment("a.py", 10, "RIGHT", "body\n<!-- ai-review:fp1 -->")])
    kinds = [k for k, _ in posts]
    assert "note" in kinds and "discussion" in kinds
    note = next(b for k, b in posts if k == "note")
    assert note["body"] == "Сводка"
    disc = next(b for k, b in posts if k == "discussion")
    assert disc["body"] == "body\n<!-- ai-review:fp1 -->"
    pos = disc["position"]
    assert pos["position_type"] == "text"
    assert pos["base_sha"] == "b1" and pos["head_sha"] == "h1" and pos["start_sha"] == "s1"
    assert pos["new_path"] == "a.py" and pos["new_line"] == 10


def test_publish_review_left_side_uses_old_line():
    posts = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/merge_requests/6"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"},
                "target_branch": "main", "source_branch": "x",
                "title": "T", "description": "", "draft": False})
        if req.method == "POST" and req.url.path.endswith("/discussions"):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d"})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404)

    p = make_provider(handler)
    p.publish_review(6, "h", "S", [InlineComment("a.py", 4, "LEFT", "b")])
    pos = posts[0]["position"]
    assert pos["old_line"] == 4 and "new_line" not in pos
