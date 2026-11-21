"""Compare test-exemplar selection on the three PRI-296 reproductions."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import asdict
from statistics import median
from time import perf_counter

from eval.solve_task_metrics.context_paths import extract_context_paths
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.index.refs import base_ref
from reviewer.mcp.service import MCPReviewService, _TaskContextDeps
from reviewer.mcp.task_context import _queries, _test_queries
from reviewer.retrieval.retriever import _is_test_path

REPO = "mimfort/rag_for_git"
BRANCH = "dev"
INDEX_SHA = "bfcefdf96b9ea67beea766aa75adf0acbbcbcdc2"
REPEATS = 10

# Existing test files matched to the tasks by behavior at INDEX_SHA.
LABELS = {
    "PRI-278": {
        "tests/eval/test_context_paths.py",
        "tests/eval/test_live_boundary.py",
        "tests/eval/test_replay.py",
        "tests/metrics/test_context_core.py",
    },
    "PRI-291": {
        "tests/mcp/test_context_limits_wiring.py",
        "tests/mcp/test_prepare_task_context.py",
        "tests/mcp/test_server.py",
        "tests/retrieval/test_multiquery.py",
    },
    "PRI-256": {
        "tests/eval/test_live_boundary.py",
        "tests/mcp/test_context_limits_wiring.py",
        "tests/policy/test_context_limits.py",
        "tests/retrieval/test_multiquery.py",
    },
}


def measure(search):
    start = perf_counter()
    context = search()
    elapsed = round((perf_counter() - start) * 1000, 3)
    return extract_context_paths(context), elapsed


def compare_task(service, key, task):
    deps = _TaskContextDeps(service, None)
    payload = service.prepare_task_context(REPO, key, BRANCH, warm_board=False)
    code = extract_context_paths(payload["code"])
    test_exemplars = extract_context_paths(payload["test_exemplars"])
    if not test_exemplars:
        raise RuntimeError(f"No test exemplars for seeded task {key}")
    queries = _test_queries(task, key)
    searches = {
        "mixed": lambda: service._search_codebase_multi(
            REPO, queries, BRANCH, include_tests=True, strict=True),
        "tests_only": lambda: deps.test_exemplars(REPO, BRANCH, queries),
    }
    samples = {mode: [] for mode in searches}
    for search in searches.values():
        measure(search)
    for i in range(REPEATS):
        modes = list(searches) if i % 2 == 0 else list(reversed(searches))
        for mode in modes:
            samples[mode].append(measure(searches[mode]))
    results = {}
    for mode, measurements in samples.items():
        first = measurements[0][0]
        if not all(paths == first for paths, _ in measurements):
            raise RuntimeError(f"Unstable path sets for {key} ({mode})")
        times = [elapsed for _, elapsed in measurements]
        results[mode] = {
            "paths": sorted(first),
            "test_files": sum(map(_is_test_path, first)),
            "labeled_hits": sorted(first & LABELS[key]),
            "code_overlap": sorted(first & code),
            "samples_ms": times,
            "median_ms": median(times),
        }
    if not code or not results["mixed"]["paths"]:
        raise RuntimeError(f"Missing comparison input for {key}")
    if set(results["tests_only"]["paths"]) != test_exemplars:
        raise RuntimeError(f"Section result differs from prepare_task_context for {key}")
    if results["tests_only"]["test_files"] != len(results["tests_only"]["paths"]):
        raise RuntimeError(f"Ordinary code in test-only results for {key}")
    return {
        "queries": len(queries),
        "queries_sha256": hashlib.sha256(json.dumps(
            queries, ensure_ascii=False).encode()).hexdigest(),
        "labels": sorted(LABELS[key]),
        "code_paths": sorted(code),
        "gaps": payload["gaps"],
        **results,
    }


def main():
    settings = Settings()
    with closing(build_components(settings)) as components:
        service = MCPReviewService(settings, components)
        ref = base_ref(BRANCH)
        if components.store.get_index_meta(REPO, ref) != INDEX_SHA:
            raise RuntimeError(f"Expected {ref} pinned at {INDEX_SHA}")
        tasks = {key: service.get_task(key, "PRI") for key in LABELS}
        if missing := [key for key, task in tasks.items() if not task]:
            raise RuntimeError(f"Tasks missing in reviewer store: {missing}")
        # Warm the production embedder's own cache with shared vectors for both
        # modes and the production code section. The timed calls still query
        # the real stores and run the normal graph, selection and rendering.
        all_queries = [query for key, task in tasks.items()
                       for query in [*_queries(task, key), *_test_queries(task, key)]]
        components.embedder.embed_queries(list(dict.fromkeys(all_queries)))
        limits = service._resolve_context_limits(REPO, BRANCH)
        if limits.code_section.max_files != 20:
            raise RuntimeError("Expected the original limit of 20 paths")
        results = {key: compare_task(service, key, task) for key, task in tasks.items()}
        if components.store.get_index_meta(REPO, ref) != INDEX_SHA:
            raise RuntimeError("Index changed during the comparison")
    print(json.dumps({
        "index_sha": INDEX_SHA,
        "embedding_model": settings.embedding_model,
        "limits": asdict(limits),
        "tasks": results,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
