"""Контракт пользовательской документации расширяемых провайдеров досок."""

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

from dotenv import dotenv_values

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.config.task_board import normalize_task_board_config
from reviewer.entrypoints.mcp_server import create_server
from reviewer.tasks.boards.registry import default_board_registry

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _skill_section(text: str, skill: str) -> str:
    marker = f"### `{skill}`"
    section = text.split(marker, maxsplit=1)[1]
    return section.split("\n### ", maxsplit=1)[0]


MATRIX_CAPABILITIES = (
    "Sync/pagination",
    "Markdown normalization",
    "Links/subtasks",
    "Attachments",
    "Single read",
    "Discovery",
    "Create/target",
    "Finish/PR link",
    "Write-through",
    "Archive metadata",
    "Retention pushdown exact-count",
    "Native-subtask writes",
)


def _matrix_rows(text: str) -> dict[str, list[str]]:
    """Строки таблицы «Capability matrix»: имя провайдера → значения колонок."""
    section = text.split("## Capability matrix", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        rows[cells[0]] = cells[1:]
    return rows


def test_board_docs_explain_structured_setup_contract():
    text = _read("docs/board-providers.md")
    section = text.split("## Configuration", 1)[1].split("## YouGile", 1)[0]
    for marker in (
        "minimum permissions",
        "read operations",
        "write operations",
        "validation",
        "selected provider",
        "reviewer init",
    ):
        assert marker in section


def test_yougile_docs_do_not_require_admin_role():
    text = _read("docs/board-providers.md")
    section = text.split("## YouGile", 1)[1].split("## YouTrack", 1)[0]
    assert "admin role is not required" in section
    assert "API-capable account" in section
    assert "allowOnlyOpenId" in section


def test_capability_matrix_has_one_complete_row_per_registered_provider():
    """Матрица — строка на провайдера; каждая capability-колонка заполнена."""
    rows = _matrix_rows(_read("docs/board-providers.md"))

    assert rows.pop("Provider", None) == list(MATRIX_CAPABILITIES)
    registry = default_board_registry()
    documented = {
        registry.get(board_type).setup.label for board_type in registry.registered_types()
    }
    assert set(rows) == documented

    normalization = MATRIX_CAPABILITIES.index("Markdown normalization")
    for label, cells in rows.items():
        assert len(cells) == len(MATRIX_CAPABILITIES), label
        assert all(cells), label
        # Нормализация описания обязана быть названа явно, а не отмечена галочкой.
        assert cells[normalization] != "✓", label


def test_capability_matrix_records_exact_archive_metadata_and_no_pushdown():
    rows = _matrix_rows(_read("docs/board-providers.md"))
    rows.pop("Provider", None)
    archive = MATRIX_CAPABILITIES.index("Archive metadata")
    pushdown = MATRIX_CAPABILITIES.index("Retention pushdown exact-count")
    exact_by_type = {
        "trello": "Exact (`closed`)",
        "kaiten": "Exact (`condition`)",
    }
    registry = default_board_registry()

    for board_type in registry.registered_types():
        label = registry.get(board_type).setup.label
        cells = rows[label]
        assert cells[archive] == exact_by_type.get(board_type, "Unknown"), board_type
        assert cells[pushdown] == "Unsupported", label


def test_provider_reference_explains_unknown_archive_fail_open_warning():
    text = _read("docs/board-providers.md")
    matrix = text.split("## Capability matrix", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]

    normalized = " ".join(matrix.split()).casefold()
    for marker in (
        "only while `include_archived: false`",
        "does not itself exclude the row",
        "warning is emitted only then",
        "age filtering runs first and may still exclude the row",
    ):
        assert marker in normalized


def test_native_subtask_matrix_matches_registry_capabilities_exactly():
    rows = _matrix_rows(_read("docs/board-providers.md"))
    rows.pop("Provider", None)
    registry = default_board_registry()
    native_subtasks = MATRIX_CAPABILITIES.index("Native-subtask writes")

    supported_types = {
        board_type
        for board_type in registry.registered_types()
        if "native_subtasks" in registry.get(board_type).capabilities
    }
    assert supported_types == {"yougile"}
    for board_type in registry.registered_types():
        spec = registry.get(board_type)
        expected = "Supported" if board_type in supported_types else "Unsupported"
        assert rows[spec.setup.label][native_subtasks] == expected


def test_decompose_task_is_in_every_public_skill_inventory():
    for rel in ("README.md", "README.ru.md"):
        section = _skill_section(_read(rel), "decompose-task")
        assert re.search(r"^[-*] \*\*Invoke:|^[-*] \*\*Вызов:", section, re.MULTILINE)
        assert "/rag-reviewer:decompose-task" in section

    agents_skills = _read("AGENTS.md").split("## Skills", maxsplit=1)[1]
    agent_inventory = set(re.findall(r"^- `([^`]+)`$", agents_skills, re.MULTILINE))
    assert "rag-reviewer:decompose-task" in agent_inventory

    plugin_inventory = (
        _read("plugin/README.md")
        .split("## Что внутри", maxsplit=1)[1]
        .split("\n## ", maxsplit=1)[0]
    )
    plugin_commands = set(re.findall(r"`(/rag-reviewer:[^`]+)`", plugin_inventory))
    assert "/rag-reviewer:decompose-task" in plugin_commands


def test_public_docs_tool_count_matches_registered_server():
    runtime_count = len(asyncio.run(create_server(MagicMock()).list_tools()))
    assert runtime_count == 41

    count_pattern = re.compile(
        r"MCP(?:-сервер| server)[^\n]*?\b(\d+)\s+(?:tools|тул)\b",
        re.IGNORECASE,
    )
    for rel in ("README.md", "README.ru.md", "AGENTS.md", "plugin/README.md"):
        documented_counts = [int(value) for value in count_pattern.findall(_read(rel))]
        assert documented_counts == [runtime_count], (rel, documented_counts, runtime_count)


def test_provider_reference_defines_generic_native_subtask_contract():
    text = _read("docs/board-providers.md")
    section = text.split("## Native subtask writes", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]

    assert re.search(
        r"create_subtasks\(parent_key, subtasks, idempotency_key,.*board_type.*"
        r"project.*provider_options.*\)",
        section,
        re.DOTALL,
    )
    assert re.search(
        r"registry(?:-owned| owned).*`native_subtasks`.*(?:get_board_targets|discovery)",
        section,
        re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"provider result.*(?:cannot|must not).*self-spoof",
        section,
        re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"no fallback.*individual (?:task )?writes",
        section,
        re.IGNORECASE | re.DOTALL,
    )
    assert "native parent `subtasks`" in section
    assert "canonical `subtask` links" in section
    assert "strict parent-and-children write-through" in section


def test_provider_reference_documents_subtask_marker_lifecycle():
    section = (
        _read("docs/board-providers.md")
        .split("## Native subtask writes", maxsplit=1)[1]
        .split("\n## ", maxsplit=1)[0]
    )
    marker = r"`reviewer-subtask:<64 lowercase hex>`"

    assert re.search(
        marker + r".*(?:visible|raw board card).*reconciliation",
        section,
        re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        marker + r".*(?:strips?|stripped|hides?|hidden).*normalized user-facing text",
        section,
        re.IGNORECASE | re.DOTALL,
    )


def test_provider_reference_structures_result_buckets_and_retry_safety():
    section = (
        _read("docs/board-providers.md")
        .split("## Native subtask writes", maxsplit=1)[1]
        .split("\n## ", maxsplit=1)[0]
    )
    result_table = section.split("### Result buckets", maxsplit=1)[1].split("\n### ", maxsplit=1)[0]
    documented_buckets = {
        match.group(1)
        for match in re.finditer(r"^\| `([^`]+)` \| [^|]+ \|$", result_table, re.MULTILINE)
    }

    assert documented_buckets == {
        "created",
        "attached",
        "unattached",
        "pending",
        "warnings",
    }
    assert re.search(
        r"same idempotency key.*same (?:full )?payload.*safe",
        section,
        re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"same idempotency key.*different payload.*conflict",
        section,
        re.IGNORECASE | re.DOTALL,
    )


def test_provider_reference_structures_durable_recovery_by_persisted_state():
    section = (
        _read("docs/board-providers.md")
        .split("## Native subtask writes", maxsplit=1)[1]
        .split("\n## ", maxsplit=1)[0]
    )
    recovery = section.split("### Durable recovery", maxsplit=1)[1].split("\n### ", maxsplit=1)[0]
    rows: dict[str, str] = {}
    for line in recovery.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows[cells[0].strip("`")] = " ".join(cells[1:])

    assert set(rows) == {"in_flight", "board_complete"}
    in_flight = rows["in_flight"]
    assert re.search(
        r"same-key retry.*reconcile.*persisted.*markers.*before any create",
        in_flight,
        re.IGNORECASE,
    )
    assert re.search(
        r"(?:no|zero) exact marker match.*multiple matches.*never.*POST",
        in_flight,
        re.IGNORECASE,
    )
    assert "`pending`" in in_flight
    assert "`manual_required=true`" in in_flight
    assert re.search(
        r"operator.*manual board verification.*repeated retry.*progress",
        in_flight,
        re.IGNORECASE,
    )

    board_complete = rows["board_complete"]
    assert re.search(
        r"only.*strict parent-and-children write-through/reindex",
        board_complete,
        re.IGNORECASE,
    )
    assert re.search(r"no child `POST`", board_complete, re.IGNORECASE)
    assert re.search(r"no parent attachment `PUT`", board_complete, re.IGNORECASE)


def test_provider_reference_documents_every_registered_credential_env():
    text = _read("docs/board-providers.md")
    registry = default_board_registry()

    for board_type in registry.registered_types():
        spec = registry.get(board_type)
        assert f"## {spec.setup.label}" in text, board_type
        for field in spec.credential_fields:
            assert field.env in text, (board_type, field.env)


def test_provider_reference_records_shared_transport_and_its_known_debt():
    text = _read("docs/board-providers.md")

    section = text.split("## Shared transport", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    for module in ("restbase.py", "pagination.py", "graphql.py", "yfm.py"):
        assert module in section
    assert "Known debt" in section
    assert "out of scope" in section


def test_provider_reference_documents_safe_credentials_and_jira_cloud_boundary():
    text = _read("docs/board-providers.md")

    for provider, fields in {
        "YouGile": ("YOUGILE_API_KEY", "YOUGILE_API_BASE"),
        "YouTrack": ("YOUTRACK_TOKEN", "YOUTRACK_BASE_URL"),
        "Jira Cloud": ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"),
    }.items():
        assert provider in text
        assert all(field in text for field in fields)
    assert "hidden input" in text
    assert "reviewer check" in text
    assert "rotation" in text.lower()
    assert "direct site URL" in text
    assert "without scopes" in text
    assert "api.atlassian.com/ex/jira" in text


def test_provider_reference_keeps_yougile_oidc_distinct_from_rest_auth():
    text = _read("docs/board-providers.md")

    oidc_section = text.split("## YouGile", maxsplit=1)[1].split("## ", maxsplit=1)[0]
    assert "OpenID Connect" in oidc_section
    assert "not an authorization flow for REST integrations" in oidc_section
    assert not re.search(r"REST\s+OAuth|OAuth\s+for\s+REST", oidc_section, re.IGNORECASE)


def test_generic_config_and_legacy_mapping_are_explicit_and_time_bounded():
    text = _read("docs/board-providers.md")

    assert "type: <registered-provider>" in text
    assert "create_target:" in text and "done_target:" in text and "options:" in text
    assert "sync_filter:" in text
    assert "max_age_days:" in text
    assert "include_archived:" in text
    assert "done_column" in text and "done_state" in text and "status_field" in text
    assert "done_target" in text and "options.status_field" in text
    assert "one compatibility release" in text
    assert "no earlier than the next breaking release" in text
    assert "future breaking-cleanup task" in text


def test_provider_reference_documents_repo_mode_sync_board_signature():
    text = _read("docs/board-providers.md")
    signatures = text.split("The public signatures are:", maxsplit=1)[1].split("```", maxsplit=2)[1]

    assert re.search(r"sync_board\(repo=None, branch=None, board=None", signatures)
    assert "sync_filter=None" in signatures
    assert "Repo mode" in text
    assert "effective" in text
    assert "explicit" in text


def test_extension_checklist_requires_complete_registration_and_contract_fixture():
    text = _read("docs/board-providers.md")

    checklist = text.split("## Adding a provider", maxsplit=1)[1]
    for required in (
        "adapter",
        "immutable spec",
        "explicit registry",
        "full contract fixture",
        "provider-specific tests",
        "documentation matrix row",
        "partial registration",
    ):
        assert required in checklist.lower()


def test_public_docs_use_registered_provider_terminology_not_a_closed_choice():
    closed_choice = re.compile(
        r"(?:type|board_type)\s*[:=].*(?:yougile\s*\|\s*youtrack|youtrack\s*\|\s*yougile)",
        re.IGNORECASE,
    )
    for rel in ("README.md", "README.ru.md", "CLAUDE.md", ".env.example", ".review.yml"):
        assert not closed_choice.search(_read(rel)), rel


def test_env_template_leaves_all_registry_credentials_unconfigured(monkeypatch):
    registry = default_board_registry()
    credential_envs = {
        env
        for board_type in registry.registered_types()
        for field in registry.get(board_type).credential_fields
        for env in (field.env, *field.aliases)
    }
    for env in credential_envs:
        monkeypatch.delenv(env, raising=False)

    template = ROOT / ".env.example"
    values = dotenv_values(template)
    assert all(values.get(env, "") == "" for env in credential_envs)
    assert not re.search(r"^[A-Z][A-Z0-9_]*=[ \t]+#", _read(".env.example"), re.MULTILINE)

    settings = Settings(_env_file=template)
    source = ProviderCredentialSource.from_settings(settings)
    assert registry.configured_types(source) == ()


def test_readmes_document_store_first_server_side_board_workflow_symmetrically():
    required = (
        "sync_board",
        "get_task(key, project=...)",
        "store-first",
        "tasks:<type>:<board>",
        "TASK_BOARD_API_KEY → YOUGILE_API_KEY",
        "TASK_BOARD_API_BASE → YOUGILE_API_BASE",
        "legacy metadata for older clients",
    )
    forbidden = ("board-mcp", "board mcp", "mcp__<board>", "tasks:<board>")

    for rel in ("README.md", "README.ru.md"):
        text = _read(rel)
        lowered = text.lower()
        for marker in required:
            assert marker.lower() in lowered, (rel, marker)
        for stale in forbidden:
            assert stale not in lowered, (rel, stale)


def test_provider_reference_scopes_legacy_aliases_to_yougile():
    text = _read("docs/board-providers.md")

    assert "TASK_BOARD_API_KEY → YOUGILE_API_KEY" in text
    assert "TASK_BOARD_API_BASE → YOUGILE_API_BASE" in text
    assert "only for YouGile" in text
    assert "new generic fields win" in text.lower()
    assert "warning" in text.lower()


def test_root_review_yml_is_parseable_generic_config_with_key_pattern():
    import yaml

    data = yaml.safe_load(_read(".review.yml"))
    config = normalize_task_board_config(data["task_board"])

    assert config is not None
    assert config.key_pattern == r"PRI-\d+"
    assert config.url_template is None


def test_readme_sync_error_hints_use_registry_setup_and_current_config_fallback():
    for rel in ("README.md", "README.ru.md"):
        text = _read(rel)
        sync_section = _skill_section(text, "sync-tasks")
        assert "TASK_BOARD_*" not in sync_section
        assert "reviewer init" in sync_section
        assert "reviewer check" in sync_section
        assert "docs/board-providers.md" in sync_section
        assert "non-secret deploy-wide fallback" in text
        assert "configured registry credentials" in text
        assert "Credentials are not returned" in text


def test_readme_solve_workflow_does_not_list_index_task():
    for rel in ("README.md", "README.ru.md"):
        text = _read(rel)
        solve_section = _skill_section(text, "solve-task")
        assert "index_task" not in solve_section
