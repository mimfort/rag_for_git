"""Guardrail: decompose-task безопасно создаёт нативные подзадачи одним batch-вызовом."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "decompose-task" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.replace("`", "").lower().split())


def test_decompose_task_frontmatter_is_trigger_only():
    text = _text()
    assert text.startswith("---\nname: decompose-task\n")
    description = re.search(r"^description: (.+)$", text, re.MULTILINE)
    assert description is not None
    value = description.group(1)
    assert value.startswith("Use when ")
    assert "you" not in value.lower().split()
    for workflow_word in ("preview", "confirm", "create", "sync", "verify", "batch"):
        assert workflow_word not in value.lower()


def test_decompose_task_is_store_first_with_one_scoped_miss_retry():
    text = _text()
    first_read = text.index("get_task(parent_key")
    assert first_read < text.index("Preview")
    lookup_phase = text[first_read : text.index("## Context")]
    flat = _flat(lookup_phase)
    assert "on miss only" in flat
    assert "exactly one" in flat
    assert "sync_board(board=<project" in lookup_phase
    assert "one retry" in flat
    assert "still missing" in flat
    assert "stop" in flat


def test_decompose_task_discovers_authoritative_native_capability_before_context():
    text = _text()
    discovery = text.index("get_board_targets(")
    context = text.index("get_task_context(")
    assert discovery < context
    capability_block = _flat(text[discovery:context])
    assert "authoritative" in capability_block
    assert "native_subtasks" in capability_block
    assert "no-write" in capability_block
    assert "stop" in capability_block


def test_decompose_task_gathers_all_context_before_drafting():
    text = _text()
    draft = text.index("## Draft")
    for call in ("get_task_context(", "search_tasks(", "search_codebase("):
        assert text.index(call) < draft


def test_decompose_task_previews_every_child_before_one_confirmation():
    text = _text()
    preview = text[text.index("## Preview") : text.index("## Write")]
    flat = _flat(preview)
    for field in ("provider", "parent", "idempotency_key", "title", "problem", "steps", "criteria"):
        assert field in preview
    assert "complete canonical body" in flat
    assert "unseen preview" in flat
    assert "exactly one explicit confirmation" in flat
    assert "whole preview" in flat
    assert "no board writes" in flat


def test_decompose_task_uses_one_native_batch_only_after_confirmation():
    text = _text()
    call = "create_subtasks("
    assert text.count(call) == 1
    assert text.index("explicit confirmation") < text.index(call)
    assert "create_task(" not in text
    write = text[text.index("## Write") :]
    flat = _flat(write)
    assert "exactly the previewed payload" in flat
    assert "same idempotency_key" in flat
    assert "individual" in flat
    assert "never fall back" in flat


def test_decompose_task_retries_only_identical_request_and_never_guesses_in_flight():
    text = _flat(_text())
    for phrase in (
        "created",
        "attached",
        "unattached",
        "pending",
        "warnings",
        "exact same payload",
        "same idempotency_key",
        "never guess",
        "in_flight",
        "never mint",
        "replacement key",
    ):
        assert phrase in text


def test_decompose_task_resyncs_and_verifies_parent_graph_and_children():
    text = _text()
    verify = text[text.index("## Verify") :]
    assert "after any confirmed child" in verify.lower()
    assert "sync_board(board=<project" in verify
    assert "get_task(parent_key" in verify
    assert "stored links" in verify.lower()
    assert "get_task_context(" in verify
    assert "get_task(child" in verify


def test_decompose_task_is_provider_agnostic_and_answers_in_russian():
    text = _text()
    lower = text.lower()
    assert "Reply in Russian" in text
    assert "yougile" not in lower
    for token in ("board_type", "project", "provider_options"):
        assert token in text


def test_decompose_task_limits_draft_and_requires_complete_children():
    text = _text().lower()
    assert "1..20" in text
    assert "nonblank" in text
    for field in ("title", "problem", "steps", "criteria"):
        assert field in text
    assert "opaque uuid" in text


def test_decompose_task_has_scannable_guidance_for_pressure_shortcuts():
    text = _text()
    assert "## Quick Reference" in text
    assert "## Example" in text
    assert "## Common Mistakes" in text
    assert "## Red Flags" in text
    lower = text.lower()
    for pressure in ("urgency", "authority", "sunk cost"):
        assert pressure in lower
    assert "already approve" in lower
    assert "five individual" in lower
    assert "capability" in lower
    assert "search_codebase" in lower


def test_readmes_document_full_decompose_flow_and_tool_count():
    for path in (ROOT / "README.md", ROOT / "README.ru.md"):
        text = path.read_text(encoding="utf-8")
        assert "/rag-reviewer:decompose-task" in text
        section = text[text.index("/rag-reviewer:decompose-task") :]
        verification = "verif" if path.name == "README.md" else "проверк"
        for token in ("preview", "confirmation", "idempotency", "sync", verification):
            assert token in section[:2500].lower(), f"{path.name}: {token}"
        assert re.search(r"\b38\b.{0,80}MCP|MCP.{0,80}\b38\b", text, re.IGNORECASE | re.DOTALL)
