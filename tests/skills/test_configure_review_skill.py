"""Guard: скилл reviewer_configure-review — интерактивная настройка контекст-слоя
.review.yml (PRI-168). Скилл автономен (только git + правка файла), редактирует
ровно контекст-слой и не клоберит чужие ключи, пересбор не запускает.
"""
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "configure-review" / "SKILL.md")


def test_skill_exists_with_frontmatter_name():
    text = SKILL.read_text(encoding="utf-8")
    assert "name: reviewer_configure-review" in text


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
    assert "youtrack" in text                       # знает про новый тип доски
    # креды НЕ пишутся скиллом — только напоминание про env
    assert "YOUTRACK_TOKEN" in text or "env деплоя" in text


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


def test_skill_suggests_rebuilds_without_running():
    text = SKILL.read_text(encoding="utf-8")
    assert "do NOT run" in text                     # не запускает пересбор сам
    assert "reviewer_sync-codebase" in text         # при смене ignore
    assert "reviewer_summarize-subsystems" in text  # при смене depth/threshold


def test_skill_asks_for_project_scope():
    text = SKILL.read_text(encoding="utf-8")
    assert "task_board.project" in text             # пишет выбор проекта
    assert "project" in text
    # предупреждение про пустой project (тянет все проекты)
    assert "все проект" in text.lower() or "all project" in text.lower()
