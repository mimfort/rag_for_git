"""Guardrail: decompose-task безопасно создаёт нативные подзадачи одним batch-вызовом."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "decompose-task" / "SKILL.md"
PRESSURE_RATIONALIZATIONS = (
    "Exactly one batch means retries are forbidden.",
    "The original preview confirmation authorizes automatic retries.",
    "No confirmed child keys means there is nothing to verify.",
    "A small preview edit can reuse the same key.",
    "A tool error is the same as an empty context result.",
)


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


def test_decompose_task_distinguishes_initial_batch_from_exact_full_retry():
    text = _text()
    flat = _flat(text)
    assert text.count("create_subtasks(") == 1
    for phrase in (
        "exactly one initial native batch request",
        "repeat the same full batch request",
        "byte-for-byte",
        "logically exact",
        "same idempotency_key",
        "not an individual request",
        "not a remaining-items request",
    ):
        assert phrase in flat


def test_decompose_task_requires_user_choice_before_exact_retry():
    text = _flat(_text())
    for phrase in (
        "no automatic retry",
        "ask the user to choose",
        "exact retry or stop",
        "original preview confirmation",
        "does not authorize",
        "explicitly request the exact retry",
        "do not require a new preview",
    ):
        assert phrase in text


def test_decompose_task_verifies_ambiguous_attempt_before_offering_retry():
    text = _text()
    verify = _flat(text[text.index("## Verify") : text.index("## Quick Reference")])
    for phrase in (
        "partial",
        "timeout",
        "in_flight",
        "no confirmed child keys",
        "available parent and context verification",
        "same-key retry",
        "only marker reconciliation mechanism",
        "board search",
    ):
        assert phrase in verify
    assert verify.index("sync_board(board=<project") < verify.index("offer the exact retry")


def test_decompose_task_rotates_key_for_prewrite_edits_but_never_for_recovery():
    text = _text()
    preview = _flat(text[text.index("## Preview") : text.index("## Write")])
    for phrase in (
        "freeze payload, order, and key",
        "any edit before the first confirmed write",
        "invalidates",
        "new opaque idempotency_key",
        "full revised preview",
        "new explicit confirmation",
    ):
        assert phrase in preview
    recovery = _flat(text[text.index("## Write") : text.index("## Verify")])
    assert "after any attempted or uncertain write" in recovery
    assert "edits and a new key are forbidden" in recovery


def test_decompose_task_attempts_required_context_and_distinguishes_empty_from_error():
    text = _text()
    lookup = _flat(text[text.index("## Lookup") : text.index("## Context")])
    assert re.search(r"parent.{0,120}required", lookup)
    assert re.search(r"native_subtasks.{0,120}required", lookup)
    context = _flat(text[text.index("## Context") : text.index("## Draft")])
    assert "attempt all three" in context
    assert "empty successful results" in context
    assert "report" in context
    assert "tool error is not an empty result" in context
    assert "stop drafting until resolved" in context
    assert "optional context" not in context


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


def test_decompose_task_repeats_exact_pressure_rationalizations_in_both_scan_sections():
    text = _text()
    mistakes = text[text.index("## Common Mistakes") : text.index("## Red Flags")].lower()
    red_flags = text[text.index("## Red Flags") :].lower()
    for rationalization in PRESSURE_RATIONALIZATIONS:
        assert rationalization.lower() in mistakes
        assert rationalization.lower() in red_flags


def test_readmes_document_full_decompose_flow_and_tool_count():
    for path in (ROOT / "README.md", ROOT / "README.ru.md"):
        text = path.read_text(encoding="utf-8")
        assert "/rag-reviewer:decompose-task" in text
        section = text[text.index("/rag-reviewer:decompose-task") :]
        verification = "verif" if path.name == "README.md" else "проверк"
        for token in ("preview", "confirmation", "idempotency", "sync", verification):
            assert token in section[:2500].lower(), f"{path.name}: {token}"
        assert re.search(r"\b38\b.{0,80}MCP|MCP.{0,80}\b38\b", text, re.IGNORECASE | re.DOTALL)
