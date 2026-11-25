"""Portable solve-task verification through the reviewer MCP boundary."""
import asyncio
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from reviewer.config.settings import Settings
from reviewer.entrypoints.mcp_server import create_server
from reviewer.index.store import Retrieved
from reviewer.mcp import brief_evidence, task_context
from reviewer.mcp.brief_anchors import check_brief_anchors
from reviewer.mcp.brief_evidence import BriefEvidence
from reviewer.mcp.schemas import BriefCitationIn
from reviewer.mcp.service import MCPReviewService
from reviewer.metrics.brief_quality import briefs
from reviewer.policy.context_limits import CodeSectionLimits, ContextLimits
from reviewer.retrieval.retriever import ContextPack

CODE = """// path with spaces.py#function (path with spaces.py:2-6)
    2 | def function():
    3 |     standard_line()
    4 |     unfinished_text(
[...truncated]"""
TEST = """// tests/test_function.py#test_it (tests/test_function.py:7-7)
    7 | def test_it(): pass"""


def _citation(path, line, section="code"):
    return BriefCitationIn(path=path, line=line, section=section)


def _git_snapshot(root, files):
    for path, source in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), "-c", "user.name=Tester",
             "-c", "user.email=tester@example.invalid", "-c", "commit.gpgsign=false", *args],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()

    git("init")
    git("add", ".")
    git("commit", "-m", "fixture")
    return git("rev-parse", "HEAD")


def _matching_snapshot(root, code_path):
    return _git_snapshot(root, {
        code_path: "# header\ndef function():\n    standard_line()\n    unfinished_text()\n",
        "tests/test_function.py": "\n" * 6 + "def test_it(): pass\n",
    })


def test_prepare_checks_displayed_lines_against_indexed_commit(monkeypatch, tmp_path):
    sha = _git_snapshot(tmp_path, {
        "code.py": "# changed position\ndef moved(): pass\ndef correct(): pass\n",
        "tests/test_code.py": "def test_it(): pass\n",
    })
    (tmp_path / "code.py").write_text("def moved(): pass\n", encoding="utf-8")
    (tmp_path / "tests/test_code.py").unlink()
    service = MCPReviewService(Settings(), MagicMock())
    monkeypatch.setattr(service, "_resolve_repo_branch", lambda repo, branch: (repo, "main"))
    code = ("// code.py#moved (code.py:1-1)\n    1 | def moved(): pass\n\n"
            "// code.py#correct (code.py:3-3)\n    3 | def correct(): pass\n\n"
            "// other.py#other (other.py:1-1)\n    1 | def other(): pass")
    test_exemplars = "// tests/test_code.py#test_it (tests/test_code.py:1-1)\n    1 | def test_it(): pass"
    monkeypatch.setattr(task_context, "build_task_context", lambda *args, **kwargs: {
        "preflight": {"indexed_sha": sha}, "code": code, "test_exemplars": test_exemplars,
        "gaps": [], "warnings": [],
    })

    payload = service.prepare_task_context(
        "owner/repo", "task", path=str(tmp_path), warm_board=False, include_brief_evidence=True,
    )
    result = service.verify_task_brief(payload["brief_evidence_id"], citations=[
        _citation("code.py", 1),
        _citation("code.py", 3),
        _citation("other.py", 1),
        _citation("tests/test_code.py", 1, "test_exemplars"),
    ])
    assert [item["status"] for item in result["citations"]] == [
        "outside_excerpt", "verified", "path_unseen", "verified",
    ]
    assert payload["brief_line_checks"] == {
        "code": {"checked": 2, "mismatched": 1, "unavailable": 1},
        "test_exemplars": {"checked": 1, "mismatched": 0, "unavailable": 0},
    }


def test_shifted_block_does_not_verify_a_matching_line_belonging_to_another_function():
    section = "// code.py#moved (code.py:1-2)\n    1 | def moved():\n    2 |     return 1"
    source = "def original():\n    return 1\n\ndef moved():\n    return 1"

    eligible, diagnostics = check_brief_anchors({"code": section}, lambda _: {"code.py": source})

    assert eligible == {}
    assert diagnostics["code"] == {"checked": 2, "mismatched": 1, "unavailable": 0}


def test_checks_visible_rows_by_section_and_ignores_board_text():
    evidence = BriefEvidence()
    key = evidence.save({
        "code": CODE, "test_exemplars": TEST,
        "related": {"linked": CODE.replace("path with spaces.py", "fabricated.py")},
    })
    result = evidence.verify(key, [
        _citation("path with spaces.py", 2),
        _citation("path with spaces.py", 4),
        _citation("path with spaces.py", 6),
        _citation("tests/test_function.py", 7, "test_exemplars"),
        _citation("tests/test_function.py", 7),
        _citation("fabricated.py", 3),
    ])

    assert result["status"] == "ok"
    assert [item["status"] for item in result["citations"]] == [
        "verified", "outside_excerpt", "outside_excerpt",
        "verified", "path_unseen", "path_unseen",
    ]


def test_snapshots_are_isolated_bounded_and_expire():
    now = 10.0
    evidence = BriefEvidence(limit=2, ttl_seconds=30, clock=lambda: now)
    first = evidence.save({"code": CODE})
    second = evidence.save({"test_exemplars": TEST})

    assert first != second
    assert evidence.verify(second, [_citation("path with spaces.py", 2)])["citations"][0][
        "status"
    ] == "section_unavailable"

    third = evidence.save({"code": CODE})
    assert evidence.verify(first, []) == {"status": "evidence_unavailable", "citations": []}
    assert evidence.verify(third, [])["status"] == "ok"
    now += 30
    restarted = BriefEvidence(limit=2, ttl_seconds=30, clock=lambda: now)
    assert restarted.verify(third, [])["status"] == "evidence_unavailable"


def test_saved_evidence_is_local_to_its_state_directory(monkeypatch, tmp_path):
    evidence = BriefEvidence()
    key = evidence.save({"code": CODE})
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "isolated-profile"))

    assert BriefEvidence().verify(key, [])["status"] == "evidence_unavailable"
    assert evidence.verify(key, [])["status"] == "ok"


def test_restart_rejects_evidence_saved_before_git_verification(tmp_path):
    current = BriefEvidence(clock=lambda: 10.0)
    current_key = current.save_sections({"code": {"example.py": {2}}})
    legacy_key = "legacy_token_without_git_verification"
    [path] = (tmp_path / "xdg-state").rglob("*.sqlite3")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO brief_evidence (id_hash, expires_at, sections) VALUES (?, ?, ?)",
            (sha256(legacy_key.encode()).digest(), 100, b'{"code":{"example.py":[1]}}'),
        )

    restarted = BriefEvidence(clock=lambda: 11.0)
    assert restarted.verify(legacy_key, [_citation("example.py", 1)]) == {
        "status": "evidence_unavailable", "citations": [],
    }
    assert restarted.verify(current_key, [_citation("example.py", 2)])["citations"][0][
        "status"
    ] == "verified"


def test_saving_purges_expired_evidence_without_refreshing_ttl(tmp_path):
    now = 1.0
    evidence = BriefEvidence(ttl_seconds=10, clock=lambda: now)
    key = evidence.save({"code": CODE})
    now += 9
    assert BriefEvidence(clock=lambda: now).verify(key, [])["status"] == "ok"
    now += 1
    second = evidence.save({"code": CODE})

    assert evidence.verify(key, [])["status"] == "evidence_unavailable"
    assert evidence.verify(second, [])["status"] == "ok"
    [path] = (tmp_path / "xdg-state").rglob("*.sqlite3")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT expires_at FROM brief_evidence").fetchall() == [(21,)]


def test_disk_cap_keeps_previous_evidence_on_oversized_write(monkeypatch, tmp_path):
    cap = 128 * 1024
    monkeypatch.setattr(brief_evidence, "_MAX_DB_BYTES", cap)
    evidence = BriefEvidence()
    key = evidence.save({"code": CODE})
    [path] = (tmp_path / "xdg-state").rglob("*.sqlite3")

    assert evidence.save_sections({"code": {"huge.py": set(range(1, cap))}}) is None
    assert evidence.verify(key, [])["status"] == "ok"
    assert path.stat().st_size <= cap


def test_default_capacity_keeps_evidence_after_64_more_prepares():
    evidence = BriefEvidence()
    first = evidence.save({"code": CODE})
    for _ in range(64):
        assert evidence.save({"test_exemplars": TEST})

    restarted = BriefEvidence()
    result = restarted.verify(first, [_citation("path with spaces.py", 2)])
    assert result["status"] == "ok"
    assert result["citations"][0]["status"] == "verified"


def test_concurrent_writers_share_one_retention_limit():
    def save(_):
        return BriefEvidence(limit=5).save({"code": CODE})

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(save, range(12)))

    assert len(set(ids)) == 12 and all(ids)
    restarted = BriefEvidence(limit=5)
    assert sum(restarted.verify(key, [])["status"] == "ok" for key in ids) == 5


def test_unavailable_state_keeps_prepare_usable(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))
    service = MCPReviewService(Settings(), MagicMock())
    monkeypatch.setattr(service, "_resolve_repo_branch", lambda repo, branch: (repo, "main"))
    clone = tmp_path / "clone"
    clone.mkdir()
    sha = _matching_snapshot(clone, "path with spaces.py")
    monkeypatch.setattr(task_context, "build_task_context", lambda *a, **kw: {
        "code": CODE, "preflight": {"indexed_sha": sha}, "gaps": [],
    })

    payload = service.prepare_task_context(
        "owner/repo", "task", path=str(clone), include_brief_evidence=True,
    )
    assert payload["code"] == CODE
    assert payload["brief_line_checks"]["code"] == {"checked": 2, "mismatched": 0, "unavailable": 0}
    assert "brief_evidence_id" not in payload
    assert service.verify_task_brief("unknown", citations=[]) == {
        "status": "evidence_unavailable", "citations": [],
    }


def test_no_id_without_line_evidence():
    evidence = BriefEvidence()
    assert evidence.save({"code": "(ничего не найдено)", "test_exemplars": None}) is None


@pytest.mark.parametrize("section", ["code", "test_exemplars"])
def test_rendered_truncation_with_a_note_excludes_partial_row(section):
    item = SimpleNamespace(
        node_id="module.py#function", path="module.py", start_line=7, end_line=9,
        text="def function():\n    full_statement()\n    return result",
    )
    pack = ContextPack(items=[item], augment_note="— additional context")
    full = pack.as_context(line_numbers=True)
    pack.max_chars = full.index("return result") + len("return")
    context = pack.as_context(line_numbers=True)
    evidence = BriefEvidence()
    key = evidence.save({section: context})

    checked = evidence.verify(key, [
        _citation("module.py", 8, section), _citation("module.py", 9, section),
    ])
    assert [citation["status"] for citation in checked["citations"]] == [
        "verified", "outside_excerpt",
    ]


def test_prepare_and_verify_are_available_through_mcp_without_retrieval_twice(monkeypatch, tmp_path):
    service = MCPReviewService(Settings(), MagicMock())
    monkeypatch.setattr(service, "_resolve_repo_branch", lambda repo, branch: (repo, "main"))
    sha = _matching_snapshot(tmp_path, "path with spaces.py")
    build = MagicMock(side_effect=lambda *a, **kw: {
        "code": CODE, "test_exemplars": TEST, "preflight": {"indexed_sha": sha}, "gaps": [],
    })
    monkeypatch.setattr(task_context, "build_task_context", build)
    server = create_server(service)

    async def call(method, args):
        output = await server.call_tool(method, args)
        return json.loads(output[0].text)

    async def scenario():
        params = {"repo": "owner/repo", "key": "task", "path": str(tmp_path), "warm_board": False}
        legacy = await call("prepare_task_context", params)
        assert "brief_evidence_id" not in legacy
        payload = await call(
            "prepare_task_context", {**params, "include_brief_evidence": True}
        )
        checked = await call("verify_task_brief", {
            "evidence_id": payload["brief_evidence_id"],
            "citations": [{"section": "code", "path": "path with spaces.py", "line": 3}],
        })
        assert checked["citations"][0]["status"] == "verified"
        assert build.call_count == 2

    asyncio.run(scenario())


def test_structured_brief_round_trips_only_verified_citations_through_mcp(monkeypatch, tmp_path):
    path = "`path with ``backticks``.py"
    service = MCPReviewService(Settings(), MagicMock())
    monkeypatch.setattr(service, "_resolve_repo_branch", lambda repo, branch: (repo, "main"))
    sha = _matching_snapshot(tmp_path, path)
    build = MagicMock(return_value={
        "code": CODE.replace("path with spaces.py", path), "test_exemplars": TEST,
        "preflight": {"indexed_sha": sha}, "gaps": [],
    })
    monkeypatch.setattr(task_context, "build_task_context", build)
    server = create_server(service)
    references = [
        {"section": "test_exemplars", "path": "tests/test_function.py", "line": 7,
         "reason": "Повторить проверку", "role": "read"},
        {"section": "code", "path": path, "line": 3,
         "reason": "Вызов упоминает `unrelated.py:9`", "role": "edit"},
        {"section": "code", "path": path, "line": 4, "reason": "Обрезанная строка"},
        {"section": "code", "path": "tests/test_function.py", "line": 7, "reason": "Тест"},
        {"section": "test_exemplars", "path": path, "line": 2, "reason": "Код"},
        {"section": "code", "path": path, "line": 2, "reason": "Определение"},
    ]

    async def scenario():
        prepared = await server.call_tool("prepare_task_context", {
            "repo": "owner/repo", "key": "task", "path": str(tmp_path), "warm_board": False,
            "include_brief_evidence": True,
        })
        verify = {
            "evidence_id": json.loads(prepared[0].text)["brief_evidence_id"],
            "references": references,
        }
        original = await server.call_tool("verify_task_brief", verify)
        other_cwd = tmp_path / "other-repo"
        other_cwd.mkdir()
        monkeypatch.chdir(other_cwd)
        restarted = create_server(MCPReviewService(Settings(), MagicMock()))
        after_restart = await restarted.call_tool("verify_task_brief", verify)
        assert json.loads(after_restart[0].text) == json.loads(original[0].text)
        return json.loads(after_restart[0].text)

    checked = asyncio.run(scenario())
    assert checked["status"] == "ok"
    assert checked["render_status"] == "ok"
    assert checked["citations"] == [
        {"role": None, **reference, "status": status}
        for reference, status in zip(references, [
            "verified", "verified", "outside_excerpt", "path_unseen", "path_unseen", "verified",
        ], strict=True)
    ]
    markdown = checked["markdown"]
    assert markdown.endswith("\n") and not markdown.endswith("\n\n")
    assert markdown.index(briefs.RELEVANT_HEADER) < markdown.index(briefs.TEST_HEADER)
    assert briefs.parse_rendered_citations(markdown, briefs.RELEVANT_HEADER) == [(path, 3), (path, 2)]
    assert briefs.parse_rendered_citations(markdown, briefs.TEST_HEADER) == [
        ("tests/test_function.py", 7),
    ]
    assert "Править: Вызов упоминает `unrelated.py:9`" in markdown
    assert "Прочитать: Повторить проверку" in markdown
    assert build.call_count == 1
    state = list((tmp_path / "xdg-state").rglob("*.sqlite3"))
    assert len(state) == 1
    persisted = state[0].read_bytes()
    assert b"standard_line" not in persisted
    assert "Повторить проверку".encode() not in persisted


@pytest.mark.parametrize("reason", [
    "", "   ", "первая строка\nвторая строка", "одна\u2028две", "строка\n", "строка\x00",
])
def test_mcp_reports_invalid_format_when_every_anchored_reason_is_malformed(reason):
    service = MCPReviewService(Settings(), MagicMock())
    server = create_server(service)
    key = service._brief_evidence.save({"code": CODE})
    reference = {"section": "code", "path": "path with spaces.py", "line": 3, "reason": reason}
    result = asyncio.run(server.call_tool("verify_task_brief", {
        "evidence_id": key, "references": [reference],
    }))

    checked = json.loads(result[0].text)
    assert checked == {
        "status": "ok", "render_status": "invalid_format", "markdown": None,
        "citations": [{**reference, "role": None, "status": "invalid_reason"}],
    }


@pytest.mark.parametrize(("path", "line", "verdict"), [
    ("path with spaces.py", 3, "invalid_reason"),
    ("path with spaces.py", 4, "outside_excerpt"),
    ("other.py", 3, "path_unseen"),
])
def test_mcp_preserves_renderable_code_and_tests_with_one_malformed_reason(path, line, verdict):
    service = MCPReviewService(Settings(), MagicMock())
    server = create_server(service)
    key = service._brief_evidence.save({"code": CODE, "test_exemplars": TEST})
    references = [
        {"section": "code", "path": "path with spaces.py", "line": 2, "reason": "Контракт"},
        {"section": "code", "path": path, "line": line, "reason": "строка\nпродолжение"},
        {"section": "test_exemplars", "path": "tests/test_function.py", "line": 7, "reason": "Сценарий"},
    ]
    result = asyncio.run(server.call_tool("verify_task_brief", {
        "evidence_id": key, "references": references,
    }))

    checked = json.loads(result[0].text)
    assert checked["status"] == "ok"
    assert checked["render_status"] == "ok"
    assert [entry["status"] for entry in checked["citations"]] == ["verified", verdict, "verified"]
    markdown = checked["markdown"]
    assert briefs.parse_rendered_citations(markdown, briefs.RELEVANT_HEADER) == [
        ("path with spaces.py", 2),
    ]
    assert briefs.parse_rendered_citations(markdown, briefs.TEST_HEADER) == [
        ("tests/test_function.py", 7),
    ]
    assert "продолжение" not in markdown


def test_mcp_ignores_malformed_reason_when_all_coordinates_are_rejected():
    service = MCPReviewService(Settings(), MagicMock())
    server = create_server(service)
    key = service._brief_evidence.save({"code": CODE})
    result = asyncio.run(server.call_tool("verify_task_brief", {
        "evidence_id": key,
        "references": [{
            "section": "code", "path": "path with spaces.py", "line": 4, "reason": "строка\nпродолжение",
        }],
    }))

    checked = json.loads(result[0].text)
    assert checked["render_status"] == "empty"
    assert checked["citations"][0]["status"] == "outside_excerpt"
    assert briefs.parse_rendered_citations(checked["markdown"], briefs.RELEVANT_HEADER) == []


def test_mcp_omits_path_that_cannot_fit_a_markdown_citation():
    service = MCPReviewService(Settings(), MagicMock())
    server = create_server(service)
    path = "name\ncontinuation.py"
    key = service._brief_evidence.save_sections({"code": {path: {1}, "good.py": {1}}})
    result = asyncio.run(server.call_tool("verify_task_brief", {
        "evidence_id": key,
        "references": [
            {"section": "code", "path": path, "line": 1, "reason": "Контракт"},
            {"section": "code", "path": "good.py", "line": 1, "reason": "Обработчик"},
        ],
    }))

    checked = json.loads(result[0].text)
    assert checked["render_status"] == "ok"
    assert [row["status"] for row in checked["citations"]] == ["invalid_path", "verified"]
    assert briefs.parse_rendered_citations(checked["markdown"], briefs.RELEVANT_HEADER) == [
        ("good.py", 1),
    ]


def test_mcp_renders_empty_code_when_no_reference_is_verified(monkeypatch):
    service = MCPReviewService(Settings(), MagicMock())
    evidence = BriefEvidence()
    monkeypatch.setattr(service, "_brief_evidence", evidence)
    server = create_server(service)
    key = evidence.save({"code": CODE})
    reference = {"section": "code", "path": "path with spaces.py", "line": 4, "reason": "Обрезана"}
    result = asyncio.run(server.call_tool("verify_task_brief", {
        "evidence_id": key, "references": [reference],
    }))

    checked = json.loads(result[0].text)
    assert checked["status"] == "ok"
    assert checked["render_status"] == "empty"
    assert checked["citations"][0]["status"] == "outside_excerpt"
    assert briefs.parse_rendered_citations(checked["markdown"], briefs.RELEVANT_HEADER) == []


def test_mcp_preserves_legacy_response_and_evidence_expiry(monkeypatch):
    now = 10.0
    service = MCPReviewService(Settings(), MagicMock())
    evidence = BriefEvidence(ttl_seconds=30, clock=lambda: now)
    monkeypatch.setattr(service, "_brief_evidence", evidence)
    server = create_server(service)
    key = evidence.save({"code": CODE})
    citation = {"section": "code", "path": "path with spaces.py", "line": 3}

    async def verify(**kwargs):
        result = await server.call_tool("verify_task_brief", {"evidence_id": key, **kwargs})
        return json.loads(result[0].text)

    assert asyncio.run(verify(citations=[citation])) == {
        "status": "ok", "citations": [{**citation, "status": "verified"}],
    }
    now += 30
    unavailable = {"status": "evidence_unavailable", "citations": []}
    assert asyncio.run(verify(references=[{**citation, "reason": "Example"}])) == unavailable
    assert asyncio.run(verify(citations=[citation])) == unavailable


@pytest.mark.parametrize("inputs", [{}, {"citations": [], "references": []}])
def test_mcp_requires_exactly_one_citation_input(monkeypatch, inputs):
    service = MCPReviewService(Settings(), MagicMock())
    evidence = BriefEvidence()
    monkeypatch.setattr(service, "_brief_evidence", evidence)
    server = create_server(service)
    key = evidence.save({"code": CODE})
    result = asyncio.run(server.call_tool("verify_task_brief", {"evidence_id": key, **inputs}))

    assert json.loads(result[0].text) == {
        "status": "ok", "citations": [], "render_status": "invalid_format", "markdown": None,
    }


def test_prepare_reports_file_selection_and_rendering_only_on_request(monkeypatch):
    service = MCPReviewService(Settings(), MagicMock())
    monkeypatch.setattr(service, "_resolve_repo_branch", lambda repo, branch: (repo, "main"))
    limits = ContextLimits(code_section=CodeSectionLimits(max_files=2, chars_per_file=16))
    monkeypatch.setattr(service, "_resolve_context_limits", lambda *args: limits)
    retriever = service.components.retriever
    retriever.graph = None
    retriever.embedder.embed_queries.return_value = [[0.1]]
    retriever.store.hybrid_search.return_value = [
        Retrieved(f"{path}#f", path, "f", "function", 1, 1, "x = 1", 0.0, bm25_hit=True)
        for path in ("a.py", "b.py", "c.py")
    ]
    monkeypatch.setattr(
        task_context, "build_task_context",
        lambda deps, **kw: {"code": deps.code(kw["repo"], kw["branch"], ["query"])},
    )
    server = create_server(service)

    async def scenario():
        args = {"repo": "owner/repo", "key": "task", "warm_board": False}
        plain = json.loads((await server.call_tool("prepare_task_context", args))[0].text)
        checked = json.loads((await server.call_tool(
            "prepare_task_context", dict(args, include_code_budget=True)
        ))[0].text)
        assert "code_budget" not in plain
        assert checked["code"] == plain["code"]
        assert checked["code_budget"] == {
            "file_limit": 2, "candidate_files": 3, "omitted_by_file_limit": 1,
            "displaced_by_augmentation": 0, "selected_files": 2,
            "visible_files": 1, "hidden_by_rendering": 1,
        }
        assert retriever.store.hybrid_search.call_count == 2
        retriever.store.hybrid_search.side_effect = RuntimeError("unavailable index")
        failed = json.loads((await server.call_tool(
            "prepare_task_context", dict(args, include_code_budget=True)
        ))[0].text)
        assert "code_budget" not in failed

    asyncio.run(scenario())
