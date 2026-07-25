import json
import httpx
from reviewer.vcs.gitlab import GitLabProvider, _new_line_map
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


# Патч для a.py: строка 10 — добавленная (new_line=10, no old_line).
# Хунк: старое начало=9,1 строка, новое начало=9,3 строки:
# контекст 9, добавленные 10+11.
_PATCH_ADDED_LINE_10 = "@@ -9,1 +9,3 @@\n line9\n+line10\n+line11\n"


def test_publish_review_posts_summary_note_and_discussion():
    """RIGHT-комментарий на добавленной строке → только new_line, без old_line."""
    posts = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/merge_requests/5"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "b1", "head_sha": "h1", "start_sha": "s1"},
                "target_branch": "main", "source_branch": "x",
                "title": "T", "description": "", "draft": False})
        if req.method == "GET" and req.url.path.endswith("/merge_requests/5/changes"):
            return httpx.Response(200, json={"changes": [
                {"old_path": "a.py", "new_path": "a.py", "diff": _PATCH_ADDED_LINE_10,
                 "new_file": False, "deleted_file": False, "renamed_file": False},
            ]})
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
    # Добавленная строка — old_line не должна быть выставлена.
    assert "old_line" not in pos


def test_publish_review_left_side_uses_old_line():
    """LEFT-комментарий → только old_line, без new_line."""
    posts = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/merge_requests/6"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"},
                "target_branch": "main", "source_branch": "x",
                "title": "T", "description": "", "draft": False})
        if req.method == "GET" and req.url.path.endswith("/merge_requests/6/changes"):
            # Патч: строка 4 удалена (LEFT), строки 3 и 5 — контекст.
            patch = "@@ -3,3 +3,2 @@\n line3\n-line4\n line5\n"
            return httpx.Response(200, json={"changes": [
                {"old_path": "a.py", "new_path": "a.py", "diff": patch,
                 "new_file": False, "deleted_file": False, "renamed_file": False},
            ]})
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


# Патч для теста контекстной строки: строка new=5 — контекст, её old=5.
# Хунк -4,3 +4,3: строка 4 контекст, строка 5 контекст, строка 6 контекст.
_PATCH_CONTEXT_LINES = "@@ -4,3 +4,3 @@\n line4\n line5\n line6\n"


def test_publish_review_right_context_line_sets_both():
    """RIGHT-комментарий на контекстной строке → position содержит и new_line, и old_line."""
    posts = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/merge_requests/8"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "b8", "head_sha": "h8", "start_sha": "s8"},
                "target_branch": "main", "source_branch": "x",
                "title": "T", "description": "", "draft": False})
        if req.method == "GET" and req.url.path.endswith("/merge_requests/8/changes"):
            return httpx.Response(200, json={"changes": [
                {"old_path": "a.py", "new_path": "a.py", "diff": _PATCH_CONTEXT_LINES,
                 "new_file": False, "deleted_file": False, "renamed_file": False},
            ]})
        if req.method == "POST" and req.url.path.endswith("/discussions"):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d8"})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404)

    p = make_provider(handler)
    # Строка new=5 — контекстная, её old_line тоже 5 (смещение 0 в этом хунке).
    p.publish_review(8, "h8", "S", [InlineComment("a.py", 5, "RIGHT", "ctx comment")])
    assert len(posts) == 1
    pos = posts[0]["position"]
    assert pos["new_line"] == 5
    assert pos["old_line"] == 5


def test_publish_review_resilient_to_failed_discussion():
    """Сбой первого discussion POST (400) не прерывает публикацию остальных и сводки."""
    posts = []
    disc_call_count = 0

    def handler(req):
        nonlocal disc_call_count
        if req.method == "GET" and req.url.path.endswith("/merge_requests/9"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "b9", "head_sha": "h9", "start_sha": "s9"},
                "target_branch": "main", "source_branch": "x",
                "title": "T", "description": "", "draft": False})
        if req.method == "GET" and req.url.path.endswith("/merge_requests/9/changes"):
            patch = "@@ -1,2 +1,2 @@\n+line1\n+line2\n"
            return httpx.Response(200, json={"changes": [
                {"old_path": "a.py", "new_path": "a.py", "diff": patch,
                 "new_file": False, "deleted_file": False, "renamed_file": False},
            ]})
        if req.method == "POST" and req.url.path.endswith("/discussions"):
            disc_call_count += 1
            if disc_call_count == 1:
                # Первый комментарий — GitLab отвечает 400.
                return httpx.Response(400, json={"message": "invalid position"})
            posts.append(("discussion", json.loads(req.content)))
            return httpx.Response(201, json={"id": "d9"})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            posts.append(("note", json.loads(req.content)))
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404)

    p = make_provider(handler)
    p.publish_review(
        9, "h9", "Сводка",
        [
            InlineComment("a.py", 1, "RIGHT", "первый комментарий"),
            InlineComment("a.py", 2, "RIGHT", "второй комментарий"),
        ],
    )
    # Второй discussion должен быть опубликован несмотря на сбой первого.
    disc_posts = [b for k, b in posts if k == "discussion"]
    note_posts = [b for k, b in posts if k == "note"]
    assert len(disc_posts) == 1
    assert disc_posts[0]["body"] == "второй комментарий"
    # Сводка также опубликована.
    assert len(note_posts) == 1
    assert note_posts[0]["body"] == "Сводка"


# ── Юнит-тест _new_line_map ──────────────────────────────────────────────────

def test_new_line_map_multi_hunk():
    """Проверяет маппинг для патча с добавлением, контекстом и удалением."""
    # Хунк 1: old=1..3, new=1..4
    #   line1 (контекст): old=1, new=1
    #   +new_line (добавленная): new=2, old=None
    #   line3 (контекст): old=2 → нет, old=2, new=3
    #   -line4 (удалённая): old=3 → не в карте
    # Хунк 2: old=10..11, new=11..12
    #   line10 (контекст): old=10, new=11
    patch = (
        "@@ -1,3 +1,4 @@\n"
        " line1\n"
        "+new_line\n"
        " line3\n"
        "-line4\n"
        "@@ -10,2 +11,2 @@\n"
        " line10\n"
        " line11\n"
    )
    result = _new_line_map(patch)
    # Контекстные строки → маппинг на old_line.
    assert result[1] == 1          # new=1, old=1 (контекст)
    assert result[3] == 2          # new=3, old=2 (контекст после добавленной)
    assert result[11] == 10        # new=11, old=10 (второй хунк)
    assert result[12] == 11        # new=12, old=11 (второй хунк)
    # Добавленная строка → None.
    assert result[2] is None
    # Удалённая строка — не попадает в карту по new_line.
    assert 4 not in result         # такого new_line нет


def test_update_pull_request_body_puts_description():
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.raw_path.decode()
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json={})

    make_provider(handler).update_pull_request_body(7, "новое тело")
    assert seen["method"] == "PUT"
    # путь проекта URL-энкодится в :id (raw_path — до декодирования)
    assert seen["path"] == "/api/v4/projects/o%2Fr/merge_requests/7"
    assert seen["json"] == {"description": "новое тело"}
