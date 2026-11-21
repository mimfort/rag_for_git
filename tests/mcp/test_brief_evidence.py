"""Portable solve-task verification through the reviewer MCP boundary."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from reviewer.config.settings import Settings
from reviewer.entrypoints.mcp_server import create_server
from reviewer.index.store import Retrieved
from reviewer.mcp import task_context
from reviewer.mcp.brief_evidence import BriefEvidence
from reviewer.mcp.schemas import BriefCitationIn
from reviewer.mcp.service import MCPReviewService
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
    assert evidence.verify(third, [])["status"] == "evidence_unavailable"


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


def test_prepare_and_verify_are_available_through_mcp_without_retrieval_twice(monkeypatch):
    service = MCPReviewService(Settings(), MagicMock())
    monkeypatch.setattr(service, "_resolve_repo_branch", lambda repo, branch: (repo, "main"))
    build = MagicMock(side_effect=lambda *a, **kw: {"code": CODE, "test_exemplars": TEST})
    monkeypatch.setattr(task_context, "build_task_context", build)
    server = create_server(service)

    async def call(method, args):
        output = await server.call_tool(method, args)
        return json.loads(output[0].text)

    async def scenario():
        params = {"repo": "owner/repo", "key": "task", "warm_board": False}
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
