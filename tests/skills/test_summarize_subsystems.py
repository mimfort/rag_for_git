import re
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "summarize-subsystems" / "SKILL.md")
_INCLUDE = re.compile(r"<!-- include: (_common/[A-Za-z0-9_-]+\.md) -->")


def _assembled_skill() -> str:
    text = SKILL.read_text(encoding="utf-8")
    skills_dir = SKILL.resolve().parents[1]
    return _INCLUDE.sub(
        lambda match: (skills_dir / match.group(1)).read_text(encoding="utf-8"),
        text,
    )


def test_skill_exists_and_mentions_tools():
    text = SKILL.read_text(encoding="utf-8")
    assert "list_subsystem_clusters" in text
    assert "index_subsystem_summary" in text


def test_skill_includes_common_blocks_that_exist():
    text = SKILL.read_text(encoding="utf-8")
    common = SKILL.resolve().parents[1] / "_common"
    import re
    includes = re.findall(r"<!-- include: (_common/[\w\-./]+) -->", text)
    assert includes, "нет include-маркеров _common"
    for inc in includes:
        assert (common.parent / inc).is_file(), f"include не найден: {inc}"


def test_skill_instructs_russian_output():
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "russian" in text or "русск" in text


def test_skill_reports_deferred():
    """Шаг 5 должен упоминать deferred и cap — чтобы скилл не усекал молча."""
    text = SKILL.read_text(encoding="utf-8")
    assert "deferred" in text, "скилл не упоминает deferred"
    assert "SUMMARY_REBUILD_CAP" in text, "скилл не упоминает env-настройку cap"


def test_skill_asks_model_choice():
    """Скилл должен предлагать выбор модели для сводок (шаг 3)."""
    text = SKILL.read_text(encoding="utf-8")
    # Фраза уникальна для шага 3 и отсутствует в SKILL.md за пределами этого шага
    assert "Ask the user which model tier to use for writing summaries" in text, (
        "шаг 3 не спрашивает пользователя о модели (уникальная фраза шага удалена)"
    )
    # Хотя бы один дешёвый вариант должен быть упомянут как дефолт
    assert any(m in text for m in ("Haiku", "Sonnet", "Fable")), (
        "скилл не предлагает дешёвую модель по умолчанию"
    )


def test_skill_dispatches_subagent_on_chosen_model():
    """Шаг 4 должен описывать диспатч субагента на выбранной модели."""
    text = SKILL.read_text(encoding="utf-8")
    # Фраза уникальна для шага 4 и отсутствует в SKILL.md за пределами этого шага
    assert "dispatch a subagent on the chosen" in text, (
        "шаг 4 не предписывает диспатч субагента на выбранной модели (уникальная фраза шага удалена)"
    )


def test_skill_has_five_pipeline_steps():
    """Пайплайн должен содержать шаг 5 (Report) — прежний шаг 4 сдвинут."""
    import re
    text = SKILL.read_text(encoding="utf-8")
    # Ищем нумерованные шаги внутри ## Pipeline
    pipeline_section = re.search(r"## Pipeline(.*?)## Grounding", text, re.DOTALL)
    assert pipeline_section, "не найдена секция Pipeline"
    steps = re.findall(r"^\d+\.", pipeline_section.group(1), re.MULTILINE)
    assert len(steps) >= 5, f"ожидалось ≥5 шагов, найдено {len(steps)}"


def test_skill_preflight_echoes_depth_and_confirms():
    text = SKILL.read_text(encoding="utf-8")
    assert "depth_source" in text, "preflight не эхо-ит depth_source"
    assert "SUMMARY_CLUSTER_DEPTH" in text, "preflight не упоминает env-настройку depth"
    assert "confirm" in text.lower(), "preflight не просит подтверждения перед прогоном"
    assert "full rebuild" in text.lower(), "нет предупреждения о полном пересборе при смене depth"


def test_skill_prunes_orphans_on_full_pass():
    text = SKILL.read_text(encoding="utf-8")
    assert "prune_subsystem_summaries" in text, "скилл не вызывает prune на полном прогоне"
    assert "orphan" in text.lower(), "скилл не упоминает осиротевшие сводки"


def test_skill_prune_passes_exact_list_snapshot_and_defers_rejection():
    text = _assembled_skill()
    normalized = " ".join(text.split())

    assert "layout_token" in text
    assert "expected_source_hashes" in text
    assert (
        "`prune_subsystem_summaries(repo, branch, layout_token, "
        "expected_source_hashes)`"
        in normalized
    )
    assert "`completed=false`" in text
    assert "count the prune as raced/partial" in normalized


def test_skill_uses_incremental_file_summary_protocol():
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "get_subsystem_summary_work" in text
    assert "stale == true OR bootstrap == true" in text
    assert "added_files + changed_files" in text
    assert "one file-summary job" in normalized
    assert "must not read unchanged" in normalized.lower()
    assert "reused_fragments" in text
    assert "moved_files" in text


def test_skill_composes_only_from_ordered_fragment_texts():
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "ordered reused/moved/new fragment texts" in text
    assert "composer must not call `Read`" in text
    assert "source-code claims absent from the fragments" in text
    assert "batch prompt must name only its own paths" in normalized


def test_skill_persists_new_fragments_and_defers_races():
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert (
        "`index_subsystem_summary(repo, branch, cluster_key, title, summary, "
        "source_hash, fragments=[new file results])`"
        in normalized
    )
    assert "`stored=false`" in text
    assert "deferred/raced" in text
    assert "must not count it as success" in text


def test_skill_reports_incremental_metrics():
    text = _assembled_skill()
    for metric in (
        "created",
        "reused",
        "removed",
        "moved",
        "deferred",
        "raced",
        "fragments_pruned",
        "embedded",
    ):
        assert metric in text, f"скилл не сообщает метрику {metric}"


def test_skill_lists_clusters_in_compact_mode_with_pagination():
    """Шаг 2 должен перечислять кластеры в сжатом режиме и обходить страницы."""
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "compact=True" in normalized, "перечисление не в сжатом режиме"
    assert "has_more" in text, "скилл не обходит страницы до конца"
    assert "offset" in text, "скилл не использует offset"
    assert "total_clusters" in text


def test_skill_gets_file_level_detail_from_summary_work_not_from_listing():
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "get_subsystem_summary_work" in text
    assert "added_files + changed_files" in text
    assert "does not carry file-level" in normalized or \
        "no file-level" in normalized, (
            "скилл не объясняет, что file-level данные берутся не из перечисления"
        )


def test_skill_full_pass_requires_walking_every_page():
    """Пагинация не override: полный проход требует has_more == false."""
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "`has_more == false`" in normalized, (
        "определение полного прохода не учитывает обход всех страниц"
    )
    assert "pagination is not an override" in normalized.lower() or \
        "Pagination is not an override" in text


_STEP_52_START = "Let pending work be exactly"
_STEP_53_START = "Build the **ordered reused/moved/new fragment texts**"


def _file_job_step() -> str:
    """Срез шага 5.2 (диспатч file-job'ов) — без соседних пунктов 5.1 и 5.3."""
    text = _assembled_skill()
    start = text.index(_STEP_52_START)
    end = text.index(_STEP_53_START, start)
    return " ".join(text[start:end].split())


def test_skill_file_job_does_not_echo_fingerprint():
    """PRI-237: job не получает и не возвращает fingerprint — это серверное значение."""
    step = _file_job_step()
    assert "`{path, summary, provenance}`" in step, (
        "шаг 5.2 не требует от job'а результат {path, summary, provenance}"
    )
    assert "never compute, guess, or return a `fingerprint`" in step, (
        "шаг 5.2 не запрещает job'у выдумывать fingerprint"
    )
    assert "plus that entry's fingerprint" not in step, (
        "шаг 5.2 снова передаёт fingerprint во вход job'а"
    )
    assert "{path, fingerprint" not in step, (
        "шаг 5.2 снова требует fingerprint в результате job'а"
    )


def test_skill_orchestrator_supplies_fingerprints():
    """PRI-237: fingerprint берётся только из ответа get_subsystem_summary_work."""
    normalized = " ".join(_assembled_skill().split())
    assert (
        "The orchestrator attaches each new fragment's `fingerprint` by joining on `path`"
        in normalized
    ), "шаг 5.3 не предписывает оркестратору подстановку fingerprint по path"
    assert (
        "authoritative `added_files` / `changed_files` entries of the "
        "`get_subsystem_summary_work` response"
        in normalized
    ), "шаг 5.3 не называет авторитетный источник fingerprint"
    assert "never from a job's answer" in normalized, (
        "шаг 5.3 не запрещает брать fingerprint из ответа субагента"
    )


def test_skill_batch_keeps_path_mismatch_rejection():
    """PRI-245: отбраковка чужого path переживает батчинг — единицей становится порция."""
    step = _file_job_step()
    assert "outside its batch" in step
    assert "re-dispatch that batch" in step
    assert "increment `raced`" in step
    assert "discard that batch's results" in step
    assert "on a second mismatch count the cluster as deferred" in step
    assert "persist nothing for it" in step


def test_skill_file_job_reads_skeletons_not_source():
    """PRI-245: вход фрагмента — скелет, а не исходник через harness-Read."""
    step = _file_job_step()
    assert "get_file_skeletons(repo, paths, branch)" in step, (
        "шаг 5.2 не переведён на скелеты"
    )
    assert "no harness `Read` of a source file" in step, (
        "шаг 5.2 не запрещает harness-Read по исходнику"
    )
    assert "no `read_file`" in step, "шаг 5.2 не запрещает PR-сессионный read_file"


def test_skill_file_job_rejects_note_or_truncated_skeleton():
    """PRI-245 финальное ревью: усечённый/отсутствующий скелет не должен молча
    стать «валидной» сводкой — job обязан отбраковать такой путь как deferred."""
    step = _file_job_step()
    assert "starts with `(`" in step, (
        "шаг 5.2 не запрещает суммаризацию скелета-ноты (значение, начинающееся с `(`)"
    )
    assert "`(…усечено)`" in step, (
        "шаг 5.2 не упоминает точный маркер усечения скелета"
    )
    assert "not source" in step
    assert "count it toward `deferred`" in step


def test_skill_batches_file_jobs():
    """PRI-245: один субагент на порцию путей, а не на файл."""
    step = _file_job_step()
    assert "batches of at most 15 paths" in step, "порция не описана или без потолка"
    assert "one file-summary job per batch" in step
    assert "a **single** `get_file_skeletons(repo, paths, branch)` call" in step, (
        "не сказано, что скелеты берутся одним вызовом на порцию"
    )


def test_skill_preflight_names_clustering_filter_as_layout_component():
    text = _assembled_skill()
    normalized = " ".join(text.split())
    assert "summary_paths.ignore" in normalized, (
        "preflight не называет фильтр кластеризации компонентом layout policy"
    )


def test_common_tool_usage_lists_get_file_skeletons():
    text = _assembled_skill()
    assert "get_file_skeletons(repo, paths, branch?)" in text
