"""Прогон ретрива по корпусу задач против ground truth (PRI-254).

Модуль намеренно не знает про живые компоненты reviewer: источник ретрива
приходит объектом-провайдером, git — инъектируемым раннером, ровно как в
build_snapshot. Поэтому весь прогон тестируется без Postgres, Neo4j и Voyage.

Замеряется СЫРОЙ ретрив, а не бриф: baseline существующего снимка построен на
путях, которые отобрала LLM, и линии replay/snapshot несравнимы напрямую.
"""
from __future__ import annotations

import pathlib
import statistics

from . import briefs, classify, context_core, context_seeds, ground_truth, recall, variants

SCHEMA = 1
"""Версия схемы снимка replay. Растёт при несовместимом изменении формата."""

STATUS_MEASURED = "measured"
STATUS_EMPTY_CORE = "empty_core_denominator"
STATUS_NO_GROUND_TRUTH = "no_ground_truth"
STATUS_NO_TASK = "task_not_in_store"
STATUS_RETRIEVAL_FAILED = "retrieval_failed"
STATUS_EMPTY_CONTEXT = "empty_context_denominator"
STATUS_CONTEXT_FAILED = "context_retrieval_failed"
STATUS_UNDEFINED_CONTEXT = "undefined_context_denominator"

STATUSES = (
    STATUS_MEASURED,
    STATUS_EMPTY_CORE,
    STATUS_NO_GROUND_TRUTH,
    STATUS_NO_TASK,
    STATUS_RETRIEVAL_FAILED,
)

CONTEXT_STATUSES = (
    STATUS_MEASURED,
    STATUS_EMPTY_CONTEXT,
    STATUS_UNDEFINED_CONTEXT,
    STATUS_CONTEXT_FAILED,
)

SEED_MODE_LINES = "lines"
SEED_MODE_LINES_SIGNATURE = "lines+signature"

SEED_MODES = (SEED_MODE_LINES, SEED_MODE_LINES_SIGNATURE)
"""Источник разрешённых имён фильтра (PRI-266).

`lines` — только имена с изменённых строк (поведение PRI-262, дефолт).
`lines+signature` — плюс имена из шапок символов-сидов. Режим, а не правка
кода между прогонами: обе стороны A/B обязаны сниматься одним исходником.
"""


def allowed_names_for(seeds, seed_mode: str) -> set:
    """Разрешённые имена фильтра для выбранного режима сидов."""
    if seed_mode == SEED_MODE_LINES_SIGNATURE:
        return seeds.called_names | seeds.signature_names
    return seeds.called_names


def context_denominator_defined(changed_paths) -> bool:
    """Мог ли обход быть засеян в принципе.

    Сиды строятся через `chunk_python`, а граф хранит символы только для
    Python. У задачи, чьё изменённое ядро целиком не-Python (или ядра нет
    вовсе), знаменатель контекста НЕОПРЕДЕЛИМ, а не пуст: «пусто» — это
    высказывание «читать нечего», и смешивать с ним «мерить нечем» значит
    считать метрику там, где точки измерения не существует.

    Считается по фактическим путям задачи, а не по результату обхода: иначе
    неопределимость маскируется пустой выдачей графа.
    """
    return any(
        classify.is_core_production_path(path) and path.endswith(".py")
        for path in changed_paths
    )


CONTEXT_EVALUATED_STATUSES = (
    STATUS_MEASURED,
    STATUS_EMPTY_CORE,
    STATUS_NO_TASK,
)
"""Статусы задач, у которых обход графа вообще выполнялся.

Задача без ground truth или с упавшим ретривом до обхода не доходит, и её
дефолтный context_status не должен подмешиваться в счётчики контекста: иначе
новый блок отчёта врёт с первого дня.
"""


def _median(values: list):
    return statistics.median(values) if values else None


def corpus_keys(briefs_dir: pathlib.Path) -> list:
    """Ключи задач корпуса в порядке брифов, по одному на ключ.

    Дедуп обязателен: два брифа одного ключа (переписанный бриф, вторая
    итерация) иначе дали бы задаче двойной вес в агрегате.
    """
    seen: set = set()
    keys: list = []
    for record in briefs.load_briefs(briefs_dir):
        if not record.task_key or record.task_key in seen:
            continue
        seen.add(record.task_key)
        keys.append(record.task_key)
    return keys


def _task_row(key: str, status: str, **fields) -> dict:
    row = {
        "key": key,
        "status": status,
        "expected": 0,
        "expected_core": 0,
        "predicted": 0,
        "hit_core": 0,
        "core_recall": None,
        "raw_recall": None,
        "precision": None,
        "predicted_paths": [],
        "expected_core_paths": [],
        "context_status": STATUS_EMPTY_CONTEXT,
        "context_core": 0,
        "hit_context": 0,
        "context_recall": None,
        "union_precision": None,
        "context_core_paths": [],
    }
    row.update(fields)
    return row


def _evaluate(key: str, predicted: set, truth, run_git, context_core_paths: set,
              context_failed: bool = False) -> dict:
    """Посчитать одну задачу той же линейкой, что build_snapshot."""
    existed_cache: dict = {}

    def existed(path: str) -> bool:
        if path not in existed_cache:
            existed_cache[path] = ground_truth.path_existed(
                truth.parent_ref, path, run_git
            )
        return existed_cache[path]

    expected_core = {
        path
        for path in truth.changed
        if classify.is_core_production_path(path) and existed(path)
    }
    row = recall.evaluate_task(
        key, predicted, truth.changed, expected_core,
        context_core=context_core_paths,
    )
    status = STATUS_MEASURED if expected_core else STATUS_EMPTY_CORE
    if context_failed:
        context_status = STATUS_CONTEXT_FAILED
    elif context_core_paths:
        context_status = STATUS_MEASURED
    elif not context_denominator_defined(truth.changed):
        context_status = STATUS_UNDEFINED_CONTEXT
    else:
        context_status = STATUS_EMPTY_CONTEXT
    return _task_row(
        key,
        status,
        expected=row.expected,
        expected_core=row.expected_core,
        predicted=row.predicted,
        hit_core=row.hit_core,
        core_recall=row.core_recall,
        raw_recall=row.raw_recall,
        precision=row.precision,
        predicted_paths=sorted(predicted),
        expected_core_paths=sorted(expected_core),
        context_status=context_status,
        context_core=row.context_core,
        hit_context=row.hit_context,
        context_recall=row.context_recall,
        union_precision=row.union_precision,
        context_core_paths=sorted(context_core_paths),
    )


def run_replay(
    *,
    provider,
    run_git,
    briefs_dir: pathlib.Path,
    target: variants.ReplayTarget,
    variant_name: str,
    commit: str,
    taken_at: str,
    limit: int | None = None,
    seed_mode: str = SEED_MODE_LINES,
) -> dict:
    """Прогнать корпус одним вариантом и вернуть снимок replay.

    Ни один отказ не прерывает прогон и не исчезает молча: у каждой задачи
    корпуса есть именованный статус (см. STATUSES).
    """
    strategy = variants.get_variant(variant_name)
    keys = corpus_keys(briefs_dir)
    partial = bool(limit) and limit < len(keys)
    if limit:
        keys = keys[:limit]

    try:
        preflight = provider.preflight(target.repo, target.branch)
    except Exception:  # noqa: BLE001 — статус индекса не обязателен для прогона
        preflight = {}

    rows: list = []
    for key in keys:
        truth = ground_truth.collect(key, run_git)
        if not truth.changed:
            rows.append(_task_row(key, STATUS_NO_GROUND_TRUTH))
            continue
        task = provider.task(key)
        # Формула запроса — продакшн-функция; она живёт в провайдере вместе с
        # остальными живыми зависимостями, чтобы этот модуль не тянул reviewer.
        task_input = variants.TaskInput(
            key=key, task=task, query=provider.query(task, key)
        )
        try:
            predicted = strategy(provider, task_input, target)
        except Exception:  # noqa: BLE001 — сбой ретрива на задаче не роняет прогон
            rows.append(_task_row(key, STATUS_RETRIEVAL_FAILED))
            continue
        context_failed = False
        try:
            seeds = context_seeds.collect_seeds(truth, run_git)
            core_now = context_core.derive_context_core(
                seeds.symbols,
                {p for p in truth.changed if classify.is_core_production_path(p)},
                lambda ids: provider.neighbors(target.repo, target.branch, ids),
                allowed_names=allowed_names_for(seeds, seed_mode),
            )
        except Exception:  # noqa: BLE001 — недоступный граф не роняет прогон корпуса
            core_now = set()
            context_failed = True
        row = _evaluate(key, predicted, truth, run_git, core_now,
                        context_failed=context_failed)
        if task is None:
            # Задача есть в корпусе брифов, но не в сторе: запрос выродился в
            # ключ, поэтому измерение непредставительно и считаться не должно.
            row["status"] = STATUS_NO_TASK
            row["core_recall"] = None
        rows.append(row)

    measured = [r for r in rows if r["status"] == STATUS_MEASURED]
    quality = [
        recall.TaskQuality(
            task_key=r["key"],
            expected=r["expected"],
            expected_core=r["expected_core"],
            predicted=r["predicted"],
            hit_core=r["hit_core"],
            core_recall=r["core_recall"],
            raw_recall=r["raw_recall"],
            precision=r["precision"],
            context_core=r["context_core"],
            hit_context=r["hit_context"],
            context_recall=r["context_recall"],
            union_precision=r["union_precision"],
        )
        for r in measured
    ]
    agg = recall.aggregate(quality)

    return {
        "schema": SCHEMA,
        "taken_at": taken_at,
        "commit": commit,
        "variant": variant_name,
        "seed_mode": seed_mode,
        "variant_params": target.limits,
        "repo": target.repo,
        "branch": preflight.get("branch", target.branch),
        "indexed_sha": preflight.get("indexed_sha"),
        "chunks": preflight.get("chunks"),
        "graph_nodes": preflight.get("graph_nodes"),
        "partial": partial,
        "corpus": len(rows),
        "statuses": {
            status: sum(1 for r in rows if r["status"] == status)
            for status in STATUSES
        },
        "context_statuses": {
            status: sum(
                1 for r in rows
                if r["status"] in CONTEXT_EVALUATED_STATUSES
                and r["context_status"] == status
            )
            for status in CONTEXT_STATUSES
        },
        "aggregate": {
            "core_recall_median": agg.core_recall_median,
            "core_recall_mean": agg.core_recall_mean,
            "raw_recall_median": agg.raw_recall_median,
            "denominator_median": agg.denominator_median,
            "bulk_core_recall_median": agg.bulk_core_recall_median,
            "bulk_n_measured": agg.bulk_n_measured,
            "n_measured": agg.n_measured,
            "no_measurement": agg.no_measurement,
            "context_recall_median": agg.context_recall_median,
            "context_n_measured": agg.context_n_measured,
            "no_context_measurement": agg.no_context_measurement,
            "union_precision_median": agg.union_precision_median,
            # Медианы уже посчитанных полей строк — не формула метрики, а сводка
            # по ним; расчётное ядро остаётся единственной копией в reviewer/.
            "precision_median": _median(
                [r["precision"] for r in measured if r["precision"] is not None]
            ),
            "predicted_median": _median([r["predicted"] for r in measured]),
        },
        "tasks": rows,
    }
