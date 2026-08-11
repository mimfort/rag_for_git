"""Guardrail: decompose-task безопасно создаёт нативные подзадачи одним batch-вызовом."""

import re
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "decompose-task" / "SKILL.md"
PRESSURE_RATIONALIZATIONS = (
    "Exactly one batch means retries are forbidden.",
    "The original preview confirmation authorizes automatic retries.",
    "No confirmed child keys means there is nothing to verify.",
    "A small preview edit can reuse the same key.",
    "A tool error is the same as an empty context result.",
    "Explicitly disabled board config can use deploy fallback.",
    "Any error is safe to retry with the same key.",
)


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.replace("`", "").lower().split())


def _load_unique_yaml(header: str) -> object:
    node = yaml.compose(header, Loader=yaml.SafeLoader)
    if isinstance(node, yaml.MappingNode):
        seen: set[tuple[str, str]] = set()
        for key_node, _ in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue
            key = (key_node.tag, key_node.value)
            if key in seen:
                raise yaml.YAMLError(f"duplicate frontmatter key: {key_node.value}")
            seen.add(key)
    return yaml.safe_load(header)


def _frontmatter(text: str) -> tuple[object, str]:
    lines = text.splitlines()
    assert lines and lines[0] == "---"
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise AssertionError("missing closing frontmatter delimiter") from error
    assert closing > 1
    parsed = _load_unique_yaml("\n".join(lines[1:closing]))
    return parsed, "\n".join(lines[closing + 1 :])


def _readme_command_section(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    command = text.index("/rag-reviewer:decompose-task")
    start = text.rfind("\n### ", 0, command)
    end = text.find("\n### ", command)
    assert start >= 0 and end > command
    return _flat(text[start:end])


def test_frontmatter_parser_rejects_invalid_yaml():
    malformed = "---\nbroken: [\n---\n# Body\n"
    with pytest.raises(yaml.YAMLError):
        _frontmatter(malformed)


@pytest.mark.parametrize("field", ("name", "description"))
def test_frontmatter_parser_rejects_duplicate_semantic_keys(field):
    duplicate = f"---\n{field}: first\n{field}: second\n---\n# Body\n"
    with pytest.raises(yaml.YAMLError, match="duplicate"):
        _frontmatter(duplicate)


def test_decompose_task_frontmatter_is_trigger_only():
    text = _text()
    header, body = _frontmatter(text)
    assert isinstance(header, Mapping)
    assert tuple(header).count("name") == 1
    assert tuple(header).count("description") == 1
    assert header["name"] == "decompose-task"
    value = header["description"]
    assert isinstance(value, str)
    assert value.startswith("Use when ")
    assert "you" not in value.lower().split()
    for workflow_word in ("preview", "confirm", "create", "sync", "verify", "batch"):
        assert workflow_word not in value.lower()
    assert body.startswith(("\n# Decompose Task", "# Decompose Task"))


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


def test_decompose_task_resolves_board_config_once_with_explicit_tristate():
    text = _text()
    lookup = _flat(text[text.index("## Lookup") : text.index("## Context")])
    for phrase in (
        "inspect the repository task_board key",
        "present with a null, empty, or disabled value",
        "explicitly disabled",
        "stop no-write",
        "never call deploy-wide get_board_config",
        "only if the repository key is absent",
        "deploy fallback",
        "call get_board_config() exactly once",
        "mapping exists",
        "freeze its generic type, project, and options",
        "never re-resolve",
    ):
        assert phrase in lookup
    assert text.count("get_board_config()") == 1


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


def test_decompose_task_preserves_metadata_and_gates_exact_retry():
    text = _text()
    write = _flat(text[text.index("## Write") : text.index("## Verify")])
    assert "retain status, category, and retryable" in write
    verify = _flat(text[text.index("## Verify") : text.index("## Quick Reference")])
    assert "display status, category, and retryable exactly as returned" in verify
    assert "after mandatory attempted-write verification" in verify
    assert "transport timeout or unknown outcome" in verify
    assert "retryable is true" in verify
    assert "retryable == false" in verify
    for category in ("unsupported", "conflict", "parent_not_found"):
        assert category in verify
    assert "stop without retry" in verify
    assert "unsupported detected before a write" in verify
    assert "no-write stop" in verify
    for phrase in (
        "same full payload",
        "same order",
        "same idempotency_key",
        "never mint",
        "never edit",
    ):
        assert phrase in verify

    quick = _flat(text[text.index("## Quick Reference") : text.index("## Example")])
    assert "absent vs explicit disable" in quick
    assert "retryable gate" in quick
    for category in ("unsupported", "conflict", "parent_not_found"):
        assert category in quick


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


def test_decompose_task_verifies_every_attempted_batch_before_outcome_or_recovery():
    text = _text()
    verify = _flat(text[text.index("## Verify") : text.index("## Quick Reference")])
    assert "after any batch write was actually attempted" in verify
    assert "regardless of status" in verify
    for status in ("ok", "partial", "error", "timeout"):
        assert status in verify
    assert "exactly one scoped" in verify
    for call in ("sync_board(board=<project", "get_task(parent_key", "get_task_context("):
        assert call in verify
    assert "get_task(child" in verify
    assert "no child keys" in verify
    assert "before declaring outcome or offering recovery" in verify
    assert "capability" in verify and "preflight" in verify
    assert "before a write" in verify and "do not trigger" in verify
    assert verify.index("sync_board(board=<project") < verify.index(
        "before declaring outcome or offering recovery"
    )
    assert "exact retry" in verify and "explicit user choice" in verify
    assert text.count("create_subtasks(") == 1

    quick = _flat(text[text.index("## Quick Reference") : text.index("## Example")])
    assert "any actually attempted batch" in quick
    assert "every status" in quick
    rationalization = "Only partial/timeout outcomes need verification."
    mistakes = text[text.index("## Common Mistakes") : text.index("## Red Flags")]
    red_flags = text[text.index("## Red Flags") :]
    assert rationalization in mistakes
    assert rationalization in red_flags


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


def test_english_readme_documents_full_decompose_safety_flow():
    path = ROOT / "README.md"
    section = _readme_command_section(path)
    for phrase in (
        "complete canonical body of every child",
        "one explicit confirmation of the whole preview",
        "every actually attempted batch write",
        "regardless of status (ok, partial, error, or timeout)",
        "before declaring its outcome or offering recovery",
        "exactly one project-scoped sync",
        "re-reads the parent with get_task",
        "graph/context with get_task_context",
        "even when no child keys were returned",
        "point-reads every returned child key with get_task",
        "partial, timeout, or error recovery is never automatic",
        "new explicit user choice",
        "exact retry or stop",
        "same full payload, order, and idempotency key",
        "never mints a new key",
        "never edits",
        "never sends only the remainder",
        "inspect the repository task_board key once",
        "present null/empty/disabled explicitly disables board work",
        "never calls deploy-wide get_board_config",
        "only an absent repository key may call get_board_config once",
        "mapping freezes generic type, project, and options for the entire flow",
        "preserves and reports status, category, and retryable",
        "transport timeout or unknown outcome or retryable=true",
        "retryable=false",
        "unsupported, conflict, and parent_not_found stop without retry",
    ):
        assert phrase in section
    assert "yougile" not in section
    text = path.read_text(encoding="utf-8")
    assert re.search(r"\b39\b.{0,80}MCP|MCP.{0,80}\b39\b", text, re.IGNORECASE | re.DOTALL)


def test_russian_readme_documents_full_decompose_safety_flow():
    path = ROOT / "README.ru.md"
    section = _readme_command_section(path)
    for phrase in (
        "полное каноническое тело каждого child",
        "одно явное confirmation всего preview",
        "каждая фактически начатая batch-запись",
        "независимо от статуса (ok, partial, error или timeout)",
        "до объявления результата или предложения recovery",
        "ровно один project-scoped sync",
        "parent через get_task",
        "graph/context через get_task_context",
        "даже если child keys не возвращены",
        "точечно читает каждый возвращённый child key через get_task",
        "recovery после partial, timeout или error никогда не запускается автоматически",
        "новый явный выбор",
        "точный retry или stop",
        "те же полные payload, order и idempotency key",
        "не создаёт новый key",
        "не редактирует",
        "не отправляет только remainder",
        "один раз проверяет repository key task_board",
        "null/empty/disabled явно отключает board work",
        "не вызывает deploy-wide get_board_config",
        "только отсутствующий repository key разрешает один вызов get_board_config",
        "mapping фиксирует generic type, project и options на весь flow",
        "сохраняет и показывает status, category и retryable",
        "transport timeout/unknown outcome или retryable=true",
        "retryable=false",
        "unsupported, conflict и parent_not_found останавливают flow без retry",
    ):
        assert phrase in section
    assert "yougile" not in section
    text = path.read_text(encoding="utf-8")
    assert re.search(r"\b39\b.{0,80}MCP|MCP.{0,80}\b39\b", text, re.IGNORECASE | re.DOTALL)
