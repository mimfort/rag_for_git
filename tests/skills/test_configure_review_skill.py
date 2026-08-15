"""Guard: скилл configure-review — интерактивная настройка контекст-слоя
.review.yml (PRI-168). Скилл автономен (только git + правка файла), редактирует
ровно контекст-слой и не клоберит чужие ключи, пересбор не запускает.
"""
import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "configure-review" / "SKILL.md")


def _branch_section() -> str:
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"## Repository branches.*?(?=\n## )", text, re.DOTALL)
    assert match
    return match.group()


def test_skill_instructs_russian_output():
    text = SKILL.read_text(encoding="utf-8")
    assert "Always answer the user in Russian" in text


def test_skill_scope_is_the_five_context_keys():
    text = SKILL.read_text(encoding="utf-8")
    for key in (
        "summary_cluster_depth",
        "summary_cluster_depth_overrides",
        "summary_topk_threshold",
        "summary_paths.ignore",
    ):
        assert key in text, f"скилл не упоминает ключ {key}"
    # Backtick-anchored: без этого `summary_paths.ignore` удовлетворяет подстроку
    # "paths.ignore" сам по себе, и удаление отдельного `paths.ignore` из скилла
    # осталось бы незамеченным.
    assert "`paths.ignore`" in text, "скилл не упоминает ключ `paths.ignore`"


def test_skill_scans_tracked_only_no_fs_walk():
    text = SKILL.read_text(encoding="utf-8")
    assert "ls-tree" in text                       # источник — трекаемые файлы
    assert "filesystem walk" in text               # ...и явный отказ от обхода ФС


def test_skill_uses_churn():
    text = SKILL.read_text(encoding="utf-8")
    assert "churn" in text
    assert "git log" in text


def test_skill_documents_untracked_junk_is_already_invisible():
    # Находка спеки §1.1: .venv/node_modules gitignored → невидимы индексу.
    text = SKILL.read_text(encoding="utf-8")
    assert ".venv" in text
    assert "gitignored" in text


def test_skill_preserves_foreign_keys():
    text = SKILL.read_text(encoding="utf-8")
    assert "categories" in text                    # пример чужого ключа, который беречь
    assert "Never clobber" in text


def test_skill_manages_task_board_block():
    text = SKILL.read_text(encoding="utf-8")
    assert "task_board" in text
    assert "create_target" in text
    assert "done_target" in text
    assert "options" in text


def test_skill_manages_finish_task_done_target():
    text = SKILL.read_text(encoding="utf-8")
    assert "done_target" in text
    assert "targets" in text
    assert "required_for" in text
    assert "choices" in text


def test_skill_asks_before_writing_ignore():
    text = SKILL.read_text(encoding="utf-8")
    assert "never write it silently" in text       # ignore — суждение, спросить


def test_skill_standalone_baseline_with_optional_count_tasks():
    text = SKILL.read_text(encoding="utf-8")
    assert "no reviewer MCP" in text                 # baseline остаётся автономным
    assert "count_tasks" in text                     # ...кроме опционального замера доски
    assert "falls back to asking" in text            # и явного фолбэка на вопрос


def test_skill_recommends_context_limits_profiles():
    text = SKILL.read_text(encoding="utf-8")
    assert "context_limits" in text
    for profile in ("tiny-util", "standard", "large / monorepo"):
        assert profile in text, f"скилл не описывает профиль {profile}"


def test_skill_context_limits_needs_no_rebuild():
    text = SKILL.read_text(encoding="utf-8")
    assert "no rebuild needed" in text


def test_skill_maps_rebuilds_to_the_changed_setting_only():
    text = SKILL.read_text(encoding="utf-8")
    rules = re.search(r"## Rebuild guidance.*?(?=\n## )", text, re.DOTALL)
    assert rules
    assert re.search(r"paths\.ignore.*rag-reviewer:sync-codebase", rules.group())
    assert re.search(r"summary_cluster_depth.*rag-reviewer:summarize-subsystems", rules.group())
    assert re.search(r"summary_cluster_depth_overrides.*rag-reviewer:summarize-subsystems", rules.group())
    assert re.search(r"summary_topk_threshold.*no rebuild", rules.group())
    assert re.search(r"context_limits.*no rebuild", rules.group())
    assert re.search(
        r"task_board\.sync_filter.*rag-reviewer:sync-tasks.*full unlimited run.*limit=null",
        rules.group(),
        re.DOTALL,
    )


def test_skill_has_complete_deterministic_context_limit_presets():
    text = SKILL.read_text(encoding="utf-8")
    presets = re.search(r"## Retrieval profile.*?(?=\n## )", text, re.DOTALL)
    assert presets
    for field in (
        "floor", "ceiling", "ratio", "abs_floor", "candidate_pool", "ann_distance_max",
        "search_tasks", "hops", "callers_topk",
    ):
        assert field in presets.group()
    for expected in ("tiny-util", "3 / 8", "standard", "4 / 15", "large / monorepo", "4 / 25"):
        assert expected in presets.group()
    for expected in ("< 150", "150–800", "800+", "3 / 10", "4 / 14"):
        assert expected in presets.group()


def test_skill_suggests_rebuilds_without_running():
    text = SKILL.read_text(encoding="utf-8")
    assert "do NOT run" in text                     # не запускает пересбор сам
    assert "rag-reviewer:sync-codebase" in text         # при смене ignore
    assert "rag-reviewer:summarize-subsystems" in text  # при смене depth/threshold
    assert "rag-reviewer:sync-tasks" in text
    assert "full unlimited run" in text
    assert "limit=null" in text


def test_skill_asks_for_project_scope():
    text = SKILL.read_text(encoding="utf-8")
    assert "task_board.project" in text             # пишет выбор проекта
    assert "project" in text
    # предупреждение про пустой project (тянет все проекты)
    assert "все проект" in text.lower() or "all project" in text.lower()


def test_skill_uses_server_side_done_target_discovery():
    text = SKILL.read_text(encoding="utf-8")
    assert "get_board_targets" in text            # server-side discovery тул
    assert "pick-list" in text                    # предъявляет список кандидатов
    # больше не зависит от клиентского yougile-MCP
    assert "get_columns" not in text


def test_skill_maps_discovery_choices_by_operation():
    text = SKILL.read_text(encoding="utf-8")
    discovery = re.search(r"get_board_targets\(.*?(?=\n## )", text, re.DOTALL)
    assert discovery
    for operation in ("sync", "create", "finish"):
        assert re.search(rf"required_for.*{operation}", discovery.group())
    assert "choices" in discovery.group()
    assert "selected `id`" in discovery.group()


def test_skill_done_target_discovery_falls_back_to_asking():
    text = SKILL.read_text(encoding="utf-8")
    # тул отсутствует/пусто/ошибка → спросить пользователя (fail-open)
    assert "fall back to asking" in text


def test_skill_uses_only_generic_board_metadata():
    text = SKILL.read_text(encoding="utf-8").lower()
    for token in ("create_target", "done_target", "options", "targets", "required_for", "choices"):
        assert token in text
    for forbidden in ("yougile", "youtrack", "done_column", "done_state", "status_field", "api_key"):
        assert forbidden not in text


def test_skill_documents_sync_filter_as_generic_task_board_sibling():
    text = SKILL.read_text(encoding="utf-8")
    board_shape = re.search(r"```yaml\n(task_board:.*?\n)```", text, re.DOTALL)

    assert board_shape
    assert re.search(
        r"(?m)^  options: \{\}\n"
        r"  sync_filter:\n"
        r"    max_age_days: <integer >= 1, or omit for no age limit>\n"
        r"    include_archived: <boolean, default true>$",
        board_shape.group(1),
    )
    assert "Never put `sync_filter` under `options`" in text
    assert "The `sync_filter` block is optional" in text


def test_skill_asks_age_and_archive_questions_separately():
    text = SKILL.read_text(encoding="utf-8")
    questions = re.search(
        r"Ask two separate questions:\s*\n(?P<body>(?:- .*\n){2})",
        text,
    )

    assert questions
    field_lines = [
        line
        for line in questions.group("body").splitlines()
        if "max_age_days" in line or "include_archived" in line
    ]
    assert len(field_lines) == 2


def test_skill_preserves_complete_task_board_block_and_comments():
    text = SKILL.read_text(encoding="utf-8")

    assert "policy layers replace the whole `task_board` block" in text
    assert re.search(r"every sibling field\s+and\s+comment", text)


def test_skill_uses_selected_board_or_materializes_lower_board_without_overlay():
    text = SKILL.read_text(encoding="utf-8")
    procedure = re.search(
        r"### Editing `sync_filter` safely.*?(?=\n### |\n## )",
        text,
        re.DOTALL,
    )

    assert procedure
    body = " ".join(procedure.group().split()).casefold()
    ordered = (
        "non-secret env/deploy `task_board` defaults",
        "home:review.yml",
        "committed `.review.yml`",
        "home:repos/<owner>/<name>.yml",
        "stop at the selected target",
    )
    positions = [body.index(marker) for marker in ordered]
    assert positions == sorted(positions)
    for sibling in (
        "type",
        "project",
        "key_pattern",
        "url_template",
        "create_target",
        "done_target",
        "options",
    ):
        assert f"`{sibling}`" in body
    assert "field-attached comments" in body
    for required in (
        "selected layer has a non-empty `task_board` mapping",
        "use that mapping alone as the edit base",
        "do not copy or overlay omitted fields from lower layers",
        "selected layer has no `task_board` key",
        "materialize the complete lower effective non-secret `task_board`",
        "never write a new partial `task_board`",
        "selected layer explicitly contains null or an empty mapping",
        "preserve that disable",
        "user explicitly chooses to replace it with a fully configured board",
        "never resurrect lower fields",
        "patch only `sync_filter`",
    ):
        assert required in body
    assert "overlay its non-secret sibling fields" not in body


def test_skill_explains_retention_semantics():
    text = SKILL.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "last-modified" in lowered
    assert "inclusive cutoff" in lowered
    assert "archive is distinct from terminal/done" in lowered
    assert "only while `include_archived: false`" in lowered
    assert "age filtering runs first" in lowered
    assert "archive warning is emitted only then" in lowered
    assert re.search(r"next successful full\s+sync", lowered)


def test_skill_warns_about_shared_corpus_and_explicit_purge():
    text = SKILL.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "same project share one task corpus" in lowered
    assert "purge remains explicit" in lowered
    assert "home per-repo" in lowered


def test_skill_keeps_filter_generic_and_credentials_out_of_policy():
    text = SKILL.read_text(encoding="utf-8")

    assert "Never put `sync_filter` under `options`" in text
    assert re.search(r"Never read, request,\s+display, or write credential values", text)


def test_skill_runs_non_disclosing_preflight_before_reading_yaml():
    text = SKILL.read_text(encoding="utf-8")
    preflight = text.index("## Safe YAML preflight")
    first_read = text.index("read the selected file", preflight)

    assert preflight < first_read
    for marker in (
        "before any tool call that can return file contents",
        "safe/blocked",
        "never matching lines, values, or exception text",
        "Do not use Read or Grep",
        "Only after a safe result",
    ):
        assert marker in text


def test_skill_rejects_ambiguous_yaml_before_mutation():
    text = SKILL.read_text(encoding="utf-8")
    preflight = text.split("## Safe YAML preflight", 1)[1].split("## Pipeline", 1)[0]

    for marker in (
        "duplicate mapping keys",
        "duplicate `repository`",
        "duplicate branch fields",
        "anchors",
        "aliases",
        "merge keys",
    ):
        assert marker in preflight
    assert "before reading or mutating" in preflight


def test_skill_preflight_rejects_symlinked_parents_and_defines_safe_create():
    text = SKILL.read_text(encoding="utf-8")
    preflight = text.split("## Safe YAML preflight", 1)[1].split("## Pipeline", 1)[0]

    for marker in (
        "every existing parent path component",
        "home config root",
        "Missing destinations",
        "including a missing home config root",
        "nearest existing parent",
        "remains inside",
        "re-check immediately before writing",
    ):
        assert marker in preflight


def test_skill_manages_primary_and_index_branches():
    section = _branch_section()
    assert "repository.primary_branch" in section
    assert "repository.index_branches" in section
    assert "ordered unique" in section
    assert "must be present in" in section


def test_skill_resolves_repo_offline_and_shows_effective_source():
    section = _branch_section()
    assert "git rev-parse --show-toplevel" in section
    assert "git remote get-url origin" in section
    assert section.count("reviewer config show --repo <owner/name> --json") >= 2
    for forbidden in ("git fetch", "git ls-remote", "git remote show"):
        assert forbidden not in section
    assert "source" in section


def test_skill_writes_branches_only_to_home_per_repo():
    section = _branch_section()
    assert "$XDG_CONFIG_HOME/rag-reviewer/repos/<owner>/<name>.yml" in section
    assert "Never write `repository` to committed `.review.yml`" in section
    assert "even when committed policy is selected" in section


def test_skill_preserves_branch_yaml_without_whole_file_dump():
    section = _branch_section()
    for marker in (
        "line-oriented patch",
        "top-level keys",
        "unknown repository subkeys",
        "comments",
        "line endings",
        "surrounding YAML style",
    ):
        assert marker in section
    assert "Never serialize the complete file with `yaml.safe_dump`" in section


def test_skill_previews_confirms_and_verifies_branch_change():
    section = _branch_section()
    assert "old and new" in section
    assert "final confirmation" in section
    assert "exact primary/index/source" in section


def test_skill_never_runs_branch_followups():
    section = _branch_section()
    assert "rag-reviewer:sync-codebase" in section
    assert "do not run" in section.lower()
    assert "rag-reviewer:summarize-subsystems" not in section
