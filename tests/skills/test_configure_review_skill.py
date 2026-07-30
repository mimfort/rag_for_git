"""Guard: скилл configure-review — интерактивная настройка контекст-слоя
.review.yml (PRI-168). Скилл автономен (только git + правка файла), редактирует
ровно контекст-слой и не клоберит чужие ключи, пересбор не запускает.
"""
import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "configure-review" / "SKILL.md")


def test_skill_instructs_russian_output():
    text = SKILL.read_text(encoding="utf-8")
    assert "Always answer the user in Russian" in text


def test_skill_scope_is_the_four_context_keys():
    text = SKILL.read_text(encoding="utf-8")
    for key in (
        "summary_cluster_depth",
        "summary_cluster_depth_overrides",
        "summary_topk_threshold",
        "paths.ignore",
    ):
        assert key in text, f"скилл не упоминает ключ {key}"


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
