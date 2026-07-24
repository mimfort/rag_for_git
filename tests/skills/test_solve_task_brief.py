"""Guardrail: solve-task фиксирует спеку brief + адаптивный relevance-фильтр (PRI-146, PRI-202).

Шаг 4 SKILL.md должен нести: скелет-шаблон brief, адаптивный (без фиксированных
числовых колпаков для Related work/Relevant code/Test exemplars — ретрив уже
ограничен server-side cliff/rails, PRI-202) relevance-фильтр, dropped-count и
бинарное правило релевантности. Тест не пинит точные формулировки — только
стабильные маркеры спеки, чтобы правка не удалила её молча.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_solve_task_brief_spec_present():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "# Brief —" in text                 # скелет-шаблон brief
    assert "No fixed ceilings" in text          # адаптивный колпак (PRI-202, не «≤N»)
    assert "bounded server-side" in text        # ретрив уже ограничен cliff/rails
    assert "(dropped" in text                   # конвенция dropped-count
    assert "directly informs" in text           # бинарное правило релевантности


def test_solve_task_passes_project_scope():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "project=" in text
    assert "task_board.project" in text


def test_solve_task_persists_brief():
    """PRI-163: шаг персиста брифа в docs/superpowers/briefs/ + ссылка на путь в хендоффе."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "docs/superpowers/briefs/" in text   # целевой путь персиста
    assert "Persist the brief" in text          # шаг персиста присутствует
    assert "file path" in text                  # хендофф ссылается на путь к файлу
    assert "Board-less" in text                 # сохранение и без ключа (slug)


def test_solve_task_dedupes_related_sources():
    """PRI-164(b): «Related work» дедупится по ключу между linked и similar."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "Dedup related sources by key" in text   # явный шаг дедупа
    assert "linked ∪ similar" in text               # оба источника, слитые
    assert "canonical task key" in text             # дедуп по каноническому ключу


def test_solve_task_handles_thin_criteria_without_provider_playbook():
    """Тонкие критерии fail-open остаются пустыми, не включая provider-specific чтение."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    section = re.search(r"\*\*Thin criteria.*?(?=\n\s*- \*\*Miss\*\*)", text, re.DOTALL)
    assert section
    assert "(?i)(критери|приёмк|acceptance)" in section.group()
    assert "leave `criteria` empty" in section.group()
    assert "Do NOT call `index_task`" in section.group()
    assert "task-context-" not in section.group()


def test_solve_task_includes_test_exemplars():
    """PRI-162: solve-task подмешивает тест-образцы (include_tests) для TDD-хендоффа."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "include_tests=True" in text     # тест-ретрив в шаге 3
    assert "Test exemplars" in text         # секция скелета брифа


def test_solve_task_lazy_expansion_present():
    """PRI-202: ленивый перевызов search_codebase с большим top_k под cliff/rails-хвост."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "Lazy expansion (no user prompt)" in text  # шаг присутствует, без интеррапта
    assert "top_k=" in text                           # перевызов с большим потолком


def test_solve_task_warns_on_existing_artifacts():
    """PRI-176: solve-task проверяет существующие briefs/specs/plans и предупреждает, не блокируя."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "*-<KEY>-*.md" in text               # glob без даты
    assert "docs/superpowers/specs/" in text    # проверка спек
    assert "docs/superpowers/plans/" in text    # проверка планов
    assert "case-insensitive" in text            # insensitive matching
    assert "[Y/n]" in text                       # предупреждение с выбором
    assert "[existing_artifacts]" in text        # тег в Constraints
    assert "Do NOT block" in text or "not block" in text  # не блокировка


def test_solve_task_step_1_5_handles_auto_permission_mode():
    """PRI-209: в auto permission mode выбор модели брифа происходит silently, иначе — спрашиваем."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    lower = text.lower()
    # Auto-mode branch exists.
    assert "if auto permission mode is active" in lower
    assert "silently choose" in lower
    # Manual branch exists.
    assert "otherwise" in lower
    assert "ask the user" in lower


def test_solve_task_asks_brief_model_choice():
    """PRI-208: Step 1.5 спрашивает у юзера tier модели для сборки брифа."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "Ask the user which model tier to use for building the brief" in text, (
        "нет шага выбора модели для брифа (уникальная фраза шага удалена)"
    )
    # Рекомендация-дефолт — mid/Sonnet-класс
    assert "mid tier (Sonnet-class)" in text, (
        "скилл не рекомендует mid/Sonnet-класс как дефолт"
    )


def test_solve_task_dispatches_brief_subagent_on_chosen_model():
    """Путь A: шаги 2–4 диспатчатся сабагентом на выбранной модели; путь B — inline-фолбэк."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "dispatch a subagent on the chosen model" in text, (
        "нет диспатча сабагента на выбранной модели (путь A)"
    )
    assert "per-subagent model override unavailable" in text, (
        "нет inline-фолбэка для CLI без per-subagent override (путь B)"
    )


def test_solve_task_records_brief_model_marker():
    """Оркестратор дописывает в бриф строку-маркер «Собран на: …»."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "Собран на" in text, (
        "нет строки-маркера «Собран на: <tier/модель>» (наблюдаемость выбора)"
    )


def test_solve_task_hints_implementations_for_oo():
    """OO/registry-хинт: directed implementations для наследников/реализаций."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "implementations" in text          # тул назван в шаге graph-deepening
    assert "IMPLEMENTS" in text or "наслед" in text  # смысл directed-обхода


def test_solve_task_uses_only_generic_board_metadata():
    text = SKILL_PATH.read_text(encoding="utf-8").lower()
    for token in ("create_target", "done_target", "options", "targets", "required_for", "choices"):
        assert token in text
    for forbidden in ("yougile", "youtrack", "done_column", "done_state", "status_field", "api_key"):
        assert forbidden not in text
