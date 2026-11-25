"""Парный ретрив PRI-294 на зафиксированных карточках и base-индексе."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import types
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from timeit import timeit

from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.index.refs import base_ref
from reviewer.mcp.service import MCPReviewService, _TaskContextDeps
from reviewer.mcp.subqueries import MAX_SUBQUERIES, build_subqueries
from reviewer.mcp.task_context import _query
from reviewer.retrieval.augment import AugmentSource
from reviewer.retrieval.retriever import _is_test_path

REPO = "mimfort/rag_for_git"
BRANCH = "dev"
BASE_SHA = "bfcefdf96b9ea67beea766aa75adf0acbbcbcdc2"
AFFECTED = ("PRI-217", "PRI-218", "PRI-220", "PRI-221", "PRI-223", "PRI-243", "PRI-269")
CONTROLS = ("PRI-207", "PRI-215", "PRI-225")
CASES = AFFECTED + CONTROLS
EVIDENCE = Path(__file__).with_name("evidence.json")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def old_builder():
    source = subprocess.check_output(
        ["git", "show", f"{BASE_SHA}:reviewer/mcp/subqueries.py"], text=True,
    )
    baseline = types.ModuleType("pri294_baseline")
    exec(compile(source, "<baseline subqueries>", "exec"), baseline.__dict__)
    return baseline.build_subqueries


def index_state(components):
    row = components.store.get_index_meta_row(REPO, base_ref(BRANCH))
    return [row[0], row[1].isoformat()] if row else None


def freeze(components, service, clone, target):
    current = index_state(components)
    if not current or current[0] != BASE_SHA:
        raise ValueError(f"Ожидался индекс {BRANCH}@{BASE_SHA}, получен {current}")
    tasks = {key: service.get_task(key, project="PRI") for key in CASES}
    if missing := [key for key, task in tasks.items() if not task]:
        raise ValueError(f"Карточки не найдены: {missing}")
    tasks = {key: {name: task[name] for name in ("title", "description", "criteria")}
             for key, task in tasks.items()}
    # В истории ревью нет обязательного сигнала. Используем штатный Git-фолбэк
    # без ленивой DDL-миграции этой таблицы и фиксируем его ввод для обоих вариантов.
    components.embedder.embed_queries([_query(task, key) for key, task in tasks.items()])
    limits = service._resolve_context_limits(REPO, BRANCH)
    candidates = {}
    for key, task in tasks.items():
        deps = _TaskContextDeps(service, str(clone), indexed_sha=current[0])
        deps.similar(_query(task, key), "PRI")
        candidates[key] = deps._augment_paths(REPO)
        if deps.augment_gaps:
            raise RuntimeError(deps.augment_gaps)
    write(target, {
        "index": current,
        "limits": asdict(limits),
        "embedding": components.embedder.model,
        "tasks": tasks,
        "augment_paths": candidates,
    })


def query_sets(task, key, arm, before):
    if arm == "baseline":
        return before(task, _query(task, key)), []
    # Диагностика кандидата подключается через тот же интерфейс, что и в MCP.
    excluded = []
    queries = build_subqueries(task, _query(task, key), excluded_criteria=excluded)
    return queries, excluded


def eligible_paths(components, evidence):
    paths = {anchor["path"] for rows in evidence.values()
             for row in rows for anchor in row["anchors"]}
    found = components.store.fetch_retrieved_at_paths(REPO, paths, base_ref=base_ref(BRANCH))
    return sorted({item.path for item in found if not _is_test_path(item.path)})


def parser_times(snapshot, before, arms, *, samples=5, calls=100):
    timings = {arm: {key: [] for key in CASES} for arm in arms}
    for ordinal, key in enumerate(CASES):
        task = snapshot["tasks"][key]
        for sample in range(samples):
            order = arms if (ordinal + sample) % 2 == 0 else arms[::-1]
            for arm in order:
                elapsed = timeit(lambda: query_sets(task, key, arm, before), number=calls)
                timings[arm][key].append(round(elapsed * 1e6 / calls, 2))
    return timings


def run(components, service, snapshot, target, arms, repeats):
    if index_state(components) != snapshot["index"]:
        raise RuntimeError("Состояние индекса изменилось с момента фиксации карточек")
    limits = service._resolve_context_limits(REPO, BRANCH)
    if asdict(limits) != snapshot["limits"] or components.embedder.model != snapshot["embedding"]:
        raise RuntimeError("Модель эмбеддингов или бюджет отличается от зафиксированного")
    candidate_sha = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", "reviewer/mcp/subqueries.py"], text=True,
    ).strip()
    before = old_builder()
    cases = {
        (key, arm): query_sets(snapshot["tasks"][key], key, arm, before)
        for key in CASES for arm in arms
    }
    # Один запрос к API для всех уникальных текстов. В сравниваемый поиск
    # эмбеддинги попадают из того же прогретого LRU, что держит reviewer.
    unique = list(dict.fromkeys(q for queries, _ in cases.values() for q in queries))
    if len(unique) > components.embedder._cache_size:
        raise RuntimeError("Парный набор не помещается в кэш эмбеддингов")
    start = perf_counter()
    components.embedder.embed_queries(unique)
    preparation = round(perf_counter() - start, 6)
    rows = {arm: {} for arm in arms}
    for ordinal, key in enumerate(CASES):
        augment = AugmentSource("similar-diffs", snapshot["augment_paths"][key],
                                limits.code_section.max_augmented_files)
        for repeat in range(repeats):
            order = arms if (ordinal + repeat) % 2 == 0 else arms[::-1]
            for arm in order:
                queries, excluded = cases[key, arm]
                if not 0 < len(queries) <= MAX_SUBQUERIES:
                    raise ValueError(f"{key}/{arm}: {len(queries)} подзапросов")
                start = perf_counter()
                context = service._search_codebase_multi(
                    REPO, queries, BRANCH, augment_sources=[augment], strict=True,
                )
                duration = round(perf_counter() - start, 6)
                if not context.startswith("// "):
                    raise RuntimeError(f"{key}/{arm}: пустая секция code")
                row = rows[arm].setdefault(key, {
                    "queries": queries, "excluded": excluded, "context": context, "seconds": [],
                })
                if row["context"] != context:
                    raise RuntimeError(f"{key}/{arm}: повтор меняет контекст")
                row["seconds"].append(duration)
            print(key, repeat + 1, flush=True)
    if index_state(components) != snapshot["index"]:
        raise RuntimeError("Состояние индекса изменилось во время сравнения")
    write(target, {
        "candidate_sha": candidate_sha,
        "eligible_paths": eligible_paths(components, read(EVIDENCE)),
        "embeddings_seconds": preparation,
        "unique_queries": len(unique),
        "parser_microseconds": parser_times(snapshot, before, arms),
        "arms": rows,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--clone", type=Path, default=Path.cwd())
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats должен быть положительным")
    logging.basicConfig(level=logging.WARNING)
    settings = Settings(review_history=False)
    with closing(build_components(settings)) as components:
        service = MCPReviewService(settings, components)
        frozen = args.work / "snapshot.json"
        if not frozen.exists():
            freeze(components, service, args.clone, frozen)
        arms = ("baseline",) if args.baseline_only else ("baseline", "candidate")
        target = args.work / ("baseline.json" if args.baseline_only else "retrieval.json")
        run(components, service, read(frozen), target, arms, args.repeats)


if __name__ == "__main__":
    main()
