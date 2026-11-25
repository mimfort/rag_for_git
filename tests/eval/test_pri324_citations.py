from __future__ import annotations

import pytest

from eval.pri324.citations import replay
from reviewer.mcp.brief_evidence import BriefEvidence

CONTEXT = (
    "// reviewer/a.py#build (reviewer/a.py:23-24)\n"
    "   23 | def build():\n"
    "   24 |     return 'строка'\n\n"
    "// reviewer/b.py#check (reviewer/b.py:57-57)\n"
    "   57 | def check():"
)
VERIFIED = {"reviewer/a.py": {23, 24}, "reviewer/b.py": {57}}


def test_aliases_keep_exact_source_and_resolve_same_coordinates():
    text, aliases = replay.alias_context(CONTEXT, VERIFIED)

    assert aliases == {
        "@001": ("reviewer/a.py", 23),
        "@002": ("reviewer/a.py", 24),
        "@003": ("reviewer/b.py", 57),
    }
    assert len(text.encode()) == len(CONTEXT.encode())
    assert text == CONTEXT.replace("   23 |", " @001 |").replace(
        "   24 |", " @002 |",
    ).replace("   57 |", " @003 |")


def test_alias_cannot_render_unverified_or_duplicate_row():
    with pytest.raises(ValueError, match="непроверенная"):
        replay.alias_context(CONTEXT, {"reviewer/a.py": {23}, "reviewer/b.py": {57}})
    duplicate = CONTEXT + "\n\n" + CONTEXT.split("\n\n")[-1]
    with pytest.raises(ValueError, match="состав"):
        replay.alias_context(duplicate, VERIFIED)


def test_unknown_alias_drops_without_losing_valid_citation(monkeypatch, tmp_path):
    monkeypatch.setattr(replay, "verified_rows", lambda *_: VERIFIED)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    briefing = {
        "key": "T-1",
        "brief": "## Relevant code\n- `@002` — Сборка ответа\n- @004 — Ошибка",
    }
    result = replay.score_case(
        {"key": "T-1", "base_sha": "abc", "context": CONTEXT},
        "alias",
        briefing,
        {"core": {"reviewer/a.py"}, "dependencies": {"reviewer/b.py"}},
        BriefEvidence(),
    )
    assert result["citations"] == [("reviewer/a.py", 24)]
    assert result["invalid"] == 1
    assert result["core"] == ["reviewer/a.py"]


def test_repeat_missing_brief_is_counted_in_the_denominator(monkeypatch, tmp_path):
    monkeypatch.setattr(replay, "verified_rows", lambda *_: VERIFIED)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    result = replay.score_case(
        {"key": "T-1", "base_sha": "abc", "context": CONTEXT},
        "control",
        None,
        {"core": {"reviewer/a.py"}, "dependencies": {"reviewer/b.py"}},
        BriefEvidence(),
    )
    assert result["citations"] == []
    total = replay.aggregate([result])
    assert total["core_total"] == 1
    assert total["core"] == 0
    assert total["missing"] == 1
