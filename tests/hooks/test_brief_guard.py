"""Unit-тесты разбора evidence для PostToolUse-хука brief_guard."""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK_PATH = ROOT / "plugin" / "hooks" / "brief_guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("brief_guard", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = previous
    return mod


bg = _load()

_SOLVE_USER = {"type": "user", "message": {"content": [
    {"type": "text", "text": "Base directory for this skill: skills/solve-task"},
]}}
_SOLVE_ATTRIBUTION = "rag-reviewer:solve-task"
_WRITE_ID = "write-brief"


def test_hooks_json_runs_single_sequential_wrapper():
    config = json.loads(
        (ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    write_entries = [
        entry
        for entry in config["hooks"]["PostToolUse"]
        if entry["matcher"] == "Write"
    ]
    commands = [
        hook["command"]
        for entry in write_entries
        for hook in entry["hooks"]
    ]

    assert len(write_entries) == 1
    assert commands == [
        'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/brief_post_write.py"'
    ]
    assert all("brief_cost.py" not in command for command in commands)
    assert all("brief_guard.py" not in command for command in commands)


def _write_transcript(tmp_path, rows):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    return transcript


def _brief(tmp_path, body):
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    brief = briefs / "task.md"
    brief.write_text(body, encoding="utf-8")
    return brief


def _tool_use(
    tool_id,
    name,
    *,
    sidechain=False,
    agent_id=None,
    attribution_skill=None,
    tool_input=None,
):
    block = {
        "type": "tool_use",
        "id": tool_id,
        "name": name,
    }
    if tool_input is not None:
        block["input"] = tool_input
    row = {"type": "assistant", "message": {"content": [block]}}
    if sidechain:
        row["isSidechain"] = True
    if agent_id is not None:
        row["agentId"] = agent_id
    if attribution_skill is not None:
        row["attributionSkill"] = attribution_skill
    return row


def _tool_result(tool_id, content, *, sidechain=False, agent_id=None):
    row = {"type": "user", "message": {"content": [{
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": content,
    }]}}
    if sidechain:
        row["isSidechain"] = True
    if agent_id is not None:
        row["agentId"] = agent_id
    return row


def _retrieval_rows(
    content,
    *,
    sidechain=False,
    tool_id="search",
    agent_id=None,
    attribution_skill=None,
):
    return [
        _tool_use(
            tool_id,
            "mcp__reviewer__search_codebase",
            sidechain=sidechain,
            agent_id=agent_id,
            attribution_skill=attribution_skill,
        ),
        _tool_result(
            tool_id,
            content,
            sidechain=sidechain,
            agent_id=agent_id,
        ),
    ]


def _write_call(
    *,
    sidechain=False,
    tool_id=_WRITE_ID,
    name="Write",
    agent_id=None,
    attribution_skill=None,
    file_path=None,
):
    tool_input = None if file_path is None else {"file_path": file_path}
    return _tool_use(
        tool_id,
        name,
        sidechain=sidechain,
        agent_id=agent_id,
        attribution_skill=attribution_skill,
        tool_input=tool_input,
    )


def _payload(brief, transcript, *, agent_id=None):
    payload = {
        "tool_name": "Write",
        "tool_use_id": _WRITE_ID,
        "tool_input": {"file_path": str(brief)},
        "transcript_path": str(transcript),
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def test_tool_result_texts_accepts_only_linked_allowed_retrieval_calls():
    rows = [
        {"type": "user", "isSidechain": True, "message": {"content": "start"}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [
            {
                "type": "tool_use",
                "id": "direct-search",
                "name": "mcp__reviewer__search_codebase",
            },
            {
                "type": "tool_use",
                "id": "plugin-graph",
                "name": "mcp__plugin_rag-reviewer_reviewer__related_symbols",
            },
            {"type": "tool_use", "id": "bash", "name": "Bash"},
            {"type": "text", "text": "// plain.py#f (plain.py:1)"},
        ]}},
        {"type": "user", "isSidechain": True, "message": {"content": [
            {
                "type": "tool_result",
                "tool_use_id": "direct-search",
                "content": json.dumps({
                    "result": "// direct.py#f (direct.py:2)",
                    "metadata": {"ignored": True},
                }),
            },
            {
                "type": "tool_result",
                "tool_use_id": "plugin-graph",
                "content": "// graph.py#f (graph.py:3)",
            },
            {
                "type": "tool_result",
                "tool_use_id": "bash",
                "content": "// fake.py#f (fake.py:4)",
            },
            {
                "type": "tool_result",
                "tool_use_id": "orphan",
                "content": "// orphan.py#f (orphan.py:5)",
            },
            {
                "type": "tool_result",
                "tool_use_id": "direct-search-typo",
                "content": "// mismatch.py#f (mismatch.py:6)",
            },
            {"type": "text", "text": "// normal.py#f (normal.py:7)"},
        ]}},
    ]

    assert bg.tool_result_texts(rows, 0) == [
        "// direct.py#f (direct.py:2)",
        "// graph.py#f (graph.py:3)",
    ]


def test_tool_result_texts_rejects_linked_result_from_assistant_row():
    rows = [
        _tool_use("retrieval", "mcp__reviewer__search_codebase"),
        {"type": "assistant", "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "retrieval",
            "content": "untrusted",
        }]}},
    ]

    assert bg.tool_result_texts(rows, -1) == []


@pytest.mark.parametrize(
    "prefix",
    ["mcp__reviewer__", "mcp__plugin_rag-reviewer_reviewer__"],
)
@pytest.mark.parametrize(
    "logical_name",
    [
        "search_codebase",
        "related_symbols",
        "callers",
        "implementations",
        "definition",
    ],
)
def test_tool_result_texts_accepts_allowed_names(prefix, logical_name):
    rows = [
        {"type": "assistant", "message": {"content": [{
            "type": "tool_use",
            "id": "retrieval",
            "name": prefix + logical_name,
        }]}},
        {"type": "user", "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "retrieval",
            "content": "trusted",
        }]}},
    ]

    assert bg.tool_result_texts(rows, -1) == ["trusted"]


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__reviewer_extra__search_codebase",
        "mcp__reviewer__search_code",
        "mcp__plugin_rag-reviewer_reviewer__unknown",
    ],
)
def test_tool_result_texts_rejects_near_prefix_and_unknown_names(tool_name):
    rows = [
        {"type": "assistant", "message": {"content": [{
            "type": "tool_use",
            "id": "untrusted",
            "name": tool_name,
        }]}},
        {"type": "user", "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": "untrusted",
            "content": "untrusted",
        }]}},
    ]

    assert bg.tool_result_texts(rows, -1) == []


@pytest.mark.parametrize(
    "sentinel",
    ["[truncated]", "[...truncated]", "[…truncated]"],
)
def test_result_is_incomplete_accepts_only_hard_truncation_sentinels(sentinel):
    assert bg._result_is_incomplete(f"retrieval output\n{sentinel}") is True


@pytest.mark.parametrize(
    "note",
    [
        "results stopped at the reranker cliff",
        "results limited by retrieval rails",
        "(truncated)",
        "(truncated 42 more)",
        "Граф ограничен внутренним лимитом результатов",
    ],
)
def test_result_is_incomplete_rejects_non_sentinel_notes(note):
    assert bg._result_is_incomplete(note) is False


def test_evidence_paths_require_code_header():
    texts = [
        "// reviewer/index/store.py#ChunkStore (reviewer/index/store.py:10-40)",
        "task mentions invented.py:20",
        "tests/test_store.py#test_x (tests/test_store.py:7)",
    ]

    assert bg.evidence_paths(texts) == {
        "reviewer/index/store.py",
        "tests/test_store.py",
    }


def test_path_matching_is_exact_or_safe_prefixed_suffix():
    observed = {
        "utils.py",
        "/tmp/worktree/reviewer/index/store.py",
        "a/utils.py",
    }

    assert bg.path_is_observed("utils.py", observed) is True
    assert bg.path_is_observed("reviewer/index/store.py", observed) is True
    assert bg.path_is_observed(
        "reviewer/index/store.py",
        {r"C:\tmp\worktree\reviewer\index\store.py"},
    ) is True
    assert bg.path_is_observed("store.py", observed) is False
    assert bg.path_is_observed("b/utils.py", observed) is False


def test_path_matching_rejects_repo_relative_prefix():
    assert bg.path_is_observed(
        "reviewer/index/store.py",
        {"other/reviewer/index/store.py"},
    ) is False


def test_guard_brief_marks_only_missing_code_and_test_paths():
    brief = (
        "## Task\n"
        "- task/spec.py:1\n"
        "\n"
        "## Relevant code\n"
        "- pkg/known.py:10\n"
        "- pkg/missing.py:20\n"
        "\n"
        "## Subsystems\n"
        "- pkg/subsystem.py:30\n"
        "\n"
        "## Test exemplars\n"
        "- tests/test_missing.py:40\n"
        "\n"
        "## Constraints\n"
        "- docs/constraint.md:50\n"
    )

    guarded, cited, missing = bg.guard_brief(brief, {"pkg/known.py"})

    assert guarded == (
        "## Task\n"
        "- task/spec.py:1\n"
        "\n"
        "## Relevant code\n"
        "- pkg/known.py:10\n"
        f"- pkg/missing.py:20 {bg.WARNING}\n"
        "\n"
        "## Subsystems\n"
        "- pkg/subsystem.py:30\n"
        "\n"
        "## Test exemplars\n"
        f"- tests/test_missing.py:40 {bg.WARNING}\n"
        "\n"
        "## Constraints\n"
        "- docs/constraint.md:50\n"
    )
    assert cited == ["pkg/known.py", "pkg/missing.py", "tests/test_missing.py"]
    assert missing == ["pkg/missing.py", "tests/test_missing.py"]


def test_guard_brief_is_idempotent_and_handles_multiple_citations():
    brief = "## Relevant code\r\n- a.py:1 calls b.py:2\r\n## Constraints\r\n"

    first, cited, missing = bg.guard_brief(brief, {"a.py"})
    second, second_cited, second_missing = bg.guard_brief(first, {"a.py"})

    assert first == f"## Relevant code\r\n- a.py:1 calls b.py:2 {bg.WARNING}\r\n## Constraints\r\n"
    assert first.count(bg.WARNING) == 1
    assert second == first
    assert cited == second_cited == ["a.py", "b.py"]
    assert missing == second_missing == ["b.py"]


def test_run_marks_only_missing_evidence_and_is_idempotent(tmp_path):
    brief = _brief(
        tmp_path,
        "## Relevant code\n- known.py:1\n- missing.py:2\n",
    )
    transcript = _write_transcript(
        tmp_path,
        [
            _SOLVE_USER,
            *_retrieval_rows("// known.py#known (known.py:1-3)"),
            _write_call(),
        ],
    )
    transcript.write_text(
        "not-json\n" + transcript.read_text(encoding="utf-8"), encoding="utf-8"
    )
    payload = _payload(brief, transcript)

    assert bg.run(payload) == 0
    first = brief.read_bytes()
    assert b"known.py:1\n" in first
    assert f"known.py:1 {bg.WARNING}".encode() not in first
    assert f"missing.py:2 {bg.WARNING}".encode() in first

    assert bg.run(payload) == 0
    assert brief.read_bytes() == first


def test_run_noop_without_evidence_or_with_truncated_result(tmp_path):
    original = "## Relevant code\n- missing.py:2\n"
    brief = _brief(tmp_path, original)
    no_evidence = _write_transcript(
        tmp_path,
        [
            _SOLVE_USER,
            *_retrieval_rows("No matching code found"),
            _write_call(),
        ],
    )

    assert bg.run(_payload(brief, no_evidence)) == 0
    assert brief.read_text(encoding="utf-8") == original

    truncated = _write_transcript(
        tmp_path,
        [
            _SOLVE_USER,
            *_retrieval_rows("// known.py#known (known.py:1)\n[...truncated]"),
            _write_call(),
        ],
    )
    assert bg.run(_payload(brief, truncated)) == 0
    assert brief.read_text(encoding="utf-8") == original


def test_run_continues_for_cliff_note_and_ignores_untrusted_truncation(tmp_path):
    brief = _brief(
        tmp_path,
        "## Relevant code\n- known.py:1\n- missing.py:2\n",
    )
    transcript = _write_transcript(
        tmp_path,
        [
            _SOLVE_USER,
            *_retrieval_rows(
                "// known.py#known (known.py:1)\nStopped at reranker cliff",
            ),
            _tool_use("shell", "Bash"),
            _tool_result("shell", "// fake.py#fake (fake.py:1)\n[...truncated]"),
            _write_call(),
        ],
    )

    assert bg.run(_payload(brief, transcript)) == 0
    guarded = brief.read_text(encoding="utf-8")
    assert f"known.py:1 {bg.WARNING}" not in guarded
    assert f"missing.py:2 {bg.WARNING}" in guarded


def test_run_noop_outside_briefs_or_without_solve_marker(tmp_path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    outside = specs / "task.md"
    original = "## Relevant code\n- missing.py:2\n"
    outside.write_text(original, encoding="utf-8")
    evidence = _retrieval_rows("// known.py#known (known.py:1)")
    transcript = _write_transcript(
        tmp_path,
        [_SOLVE_USER, *evidence, _write_call()],
    )

    assert bg.run(_payload(outside, transcript)) == 0
    assert outside.read_text(encoding="utf-8") == original

    brief = _brief(tmp_path, original)
    no_marker = _write_transcript(tmp_path, [*evidence, _write_call()])
    assert bg.run(_payload(brief, no_marker)) == 0
    assert brief.read_text(encoding="utf-8") == original


def test_run_path_a_scans_sidechain_without_solve_marker(tmp_path):
    brief = _brief(
        tmp_path,
        "## Relevant code\n- known.py:1\n- missing.py:2\n",
    )
    transcript = _write_transcript(
        tmp_path,
        [
            *_retrieval_rows(
                "// known.py#known (known.py:1)",
                sidechain=True,
                agent_id="agent-1",
                attribution_skill=_SOLVE_ATTRIBUTION,
            ),
            _write_call(
                sidechain=True,
                agent_id="agent-1",
                attribution_skill=_SOLVE_ATTRIBUTION,
                file_path=str(brief),
            ),
        ],
    )

    assert bg.run(_payload(brief, transcript, agent_id="agent-1")) == 0
    guarded = brief.read_text(encoding="utf-8")
    assert f"known.py:1 {bg.WARNING}" not in guarded
    assert f"missing.py:2 {bg.WARNING}" in guarded


@pytest.mark.parametrize(
    ("current_overrides", "payload_overrides"),
    [
        ({"attribution_skill": "other:skill"}, {}),
        ({"attribution_skill": None}, {}),
        ({"agent_id": "agent-2"}, {}),
        ({"name": "Read"}, {}),
        ({"name": "Read"}, {"tool_name": "Read"}),
        ({}, {"tool_name": "Read"}),
        ({"file_path": "/different/task.md"}, {}),
    ],
    ids=[
        "unrelated-attribution",
        "missing-attribution",
        "different-agent",
        "different-block-tool-name",
        "matching-non-write-tool-name",
        "different-payload-tool-name",
        "different-file-path",
    ],
)
def test_run_markerless_path_a_requires_current_write_provenance(
    tmp_path,
    current_overrides,
    payload_overrides,
):
    original = "## Relevant code\n- missing.py:2\n"
    brief = _brief(tmp_path, original)
    current = {
        "sidechain": True,
        "agent_id": "agent-1",
        "attribution_skill": _SOLVE_ATTRIBUTION,
        "file_path": str(brief),
    }
    current.update(current_overrides)
    transcript = _write_transcript(
        tmp_path,
        [
            *_retrieval_rows(
                "// known.py#known (known.py:1)",
                sidechain=True,
                agent_id="agent-1",
                attribution_skill=_SOLVE_ATTRIBUTION,
            ),
            _write_call(**current),
        ],
    )
    payload = _payload(brief, transcript, agent_id="agent-1")
    payload.update(payload_overrides)

    assert bg.run(payload) == 0
    assert brief.read_text(encoding="utf-8") == original


def test_run_markerless_path_a_uses_only_current_write_provenance(tmp_path):
    original = "## Relevant code\n- missing.py:2\n"
    brief = _brief(tmp_path, original)
    transcript = _write_transcript(
        tmp_path,
        [
            *_retrieval_rows(
                "// known.py#known (known.py:1)",
                sidechain=True,
                agent_id="agent-1",
                attribution_skill=_SOLVE_ATTRIBUTION,
            ),
            _write_call(
                sidechain=True,
                tool_id="older-write",
                agent_id="agent-1",
                attribution_skill=_SOLVE_ATTRIBUTION,
                file_path=str(brief),
            ),
            _write_call(
                sidechain=True,
                agent_id="agent-1",
                attribution_skill="other:skill",
                file_path=str(brief),
            ),
        ],
    )

    assert bg.run(_payload(brief, transcript, agent_id="agent-1")) == 0
    assert brief.read_text(encoding="utf-8") == original


def test_run_uses_last_solve_marker_for_main_transcript(tmp_path):
    brief = _brief(
        tmp_path,
        "## Relevant code\n- old.py:1\n- current.py:2\n",
    )
    transcript = _write_transcript(
        tmp_path,
        [
            _SOLVE_USER,
            *_retrieval_rows("// old.py#old (old.py:1)", tool_id="old-search"),
            _SOLVE_USER,
            *_retrieval_rows(
                "// current.py#current (current.py:2)",
                tool_id="current-search",
            ),
            _write_call(),
        ],
    )

    assert bg.run(_payload(brief, transcript)) == 0
    guarded = brief.read_text(encoding="utf-8")
    assert f"old.py:1 {bg.WARNING}" in guarded
    assert f"current.py:2 {bg.WARNING}" not in guarded


def test_run_waits_until_current_write_call_is_in_transcript(tmp_path, monkeypatch):
    brief = _brief(tmp_path, "## Relevant code\n- missing.py:2\n")
    transcript = tmp_path / "session.jsonl"
    earlier_rows = [
        _SOLVE_USER,
        *_retrieval_rows("// known.py#known (known.py:1)"),
    ]
    snapshots = [earlier_rows, [*earlier_rows, _write_call()]]
    reads = []

    def read_snapshot(path):
        reads.append(path)
        return snapshots[min(len(reads) - 1, len(snapshots) - 1)]

    monkeypatch.setattr(bg, "_read_jsonl", read_snapshot)
    monkeypatch.setattr("time.sleep", lambda _delay: None)

    assert bg.run(_payload(brief, transcript)) == 0
    assert reads == [str(transcript), str(transcript)]
    assert f"missing.py:2 {bg.WARNING}" in brief.read_text(encoding="utf-8")


def test_run_noops_when_current_write_call_never_reaches_transcript(
    tmp_path,
    monkeypatch,
):
    original = "## Relevant code\n- missing.py:2\n"
    brief = _brief(tmp_path, original)
    transcript = tmp_path / "session.jsonl"
    stale_rows = [
        _SOLVE_USER,
        *_retrieval_rows("// known.py#known (known.py:1)"),
    ]
    reads = []

    def read_stale(path):
        reads.append(path)
        return stale_rows

    monkeypatch.setattr(bg, "_read_jsonl", read_stale)
    monkeypatch.setattr("time.sleep", lambda _delay: None)

    assert bg.run(_payload(brief, transcript)) == 0
    assert reads == [str(transcript)] * 3
    assert brief.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("tool_use_id", [None, ""])
def test_run_noops_without_nonempty_current_tool_use_id(tmp_path, tool_use_id):
    brief = _brief(tmp_path, "## Relevant code\n- missing.py:2\n")
    transcript = _write_transcript(
        tmp_path,
        [
            _SOLVE_USER,
            *_retrieval_rows("// known.py#known (known.py:1)"),
            _write_call(),
        ],
    )
    payload = _payload(brief, transcript)
    if tool_use_id is None:
        payload.pop("tool_use_id")
    else:
        payload["tool_use_id"] = tool_use_id
    original = brief.read_bytes()

    assert bg.run(payload) == 0
    assert brief.read_bytes() == original


def test_run_debug_reports_observed_cited_and_missing(tmp_path, monkeypatch, capsys):
    brief = _brief(tmp_path, "## Relevant code\n- missing.py:2\n")
    transcript = _write_transcript(
        tmp_path,
        [
            _SOLVE_USER,
            *_retrieval_rows("// known.py#known (known.py:1)"),
            _write_call(),
        ],
    )
    monkeypatch.setenv("BRIEF_GUARD_DEBUG", "1")

    assert bg.run(_payload(brief, transcript)) == 0

    stderr = capsys.readouterr().err
    assert "observed: known.py" in stderr
    assert "cited: missing.py" in stderr
    assert "missing: missing.py" in stderr
