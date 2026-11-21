"""Keep the paired replay from increasing context along with candidate depth."""
import subprocess
import sys
from shutil import copyfile
from types import SimpleNamespace

import pytest

from eval.pri278 import briefs, pool_report, replay, report


def test_wider_input_pool_preserves_fusion_and_context_budgets(monkeypatch):
    items = {
        i: replay.Retrieved(f"src/path_{i}.py#f", f"src/path_{i}.py", "f", "function",
                            1, 1, "def f(): pass", 0.0)
        for i in range(1, 61)
    }
    prepared = replay.PreparedCase(items, {i.node_id: i for i in items.values()}, {},
                                   len(items), 0, 0, 0)
    comp = SimpleNamespace(embedder=SimpleNamespace(model="embed"),
                           reranker=SimpleNamespace(model="rerank"))

    def fake_search(conn, query, vector, pool, production_only):
        assert pool == 60
        ranked = [(i, 1 / i) for i in items]
        return ranked, ranked, 1.0, 2.0

    monkeypatch.setattr(replay, "search", fake_search)
    row = replay.evaluate(
        {"key": "PRI-0", "base_sha": "sha"}, prepared, comp, None,
        [("query", [0.0], 3.0)], (60, False), ("hybrid",), merge_limit=30,
    )

    trace, = row["traces"]
    assert row["merge_limit"] == 30
    assert len(trace["bm25"]) == len(trace["ann"]) == 60
    assert len(trace["fused"]) == 30
    assert len(row["modes"]["hybrid"]["context_paths"]) == 20


def test_explicitly_rejected_reference_does_not_count_as_selected():
    raw = {"key": "PRI-0", "brief": """## Relevant code
- src/keep.py:1 — цель правки
- src/drop.py:1 — Отброшено: пример другого механизма
"""}
    case = {"key": "PRI-0", "context": "// src/keep.py#f (src/keep.py:1-2)"}
    checked, = briefs.check_briefs([raw], [case])
    assert checked["paths"] == ["src/keep.py"]
    assert checked["invalid_references"] == []


def test_only_distinct_references_to_complete_visible_lines_count():
    raw = {"key": "PRI-0", "brief": """## Relevant code
- src/keep.py:1 — видимая строка из первого фрагмента
- src/keep.py:1 — повтор первой строки
- src/keep.py:6 — видимая строка из второго фрагмента
- src/keep.py:2 — строка заявлена только в заголовке
- src/keep.py:2 — повтор неподтверждённой строки
- src/cut.py:10 — строка была обрезана
- src/missing.py:5 — путь отсутствует
- src/keep.py:3 — Исключено: не относится к задаче
"""}
    case = {"key": "PRI-0", "context": """// src/keep.py#f (src/keep.py:1-4)
1 | def f(): pass

// src/keep.py#g (src/keep.py:6-8)
6 | def g(): pass

// src/cut.py#h (src/cut.py:10-12)
10 | def
[...truncated]
"""}
    checked, = briefs.check_briefs([raw], [case])

    assert checked["brief_coverage_version"] == "visible-lines-v2"
    assert checked["verified_paths"] == ["src/keep.py"]
    assert checked["citation_counts"] == {"verified": 2, "path_unseen": 1, "outside_excerpt": 2}


def test_visible_coverage_uses_markdown_even_if_saved_paths_and_diagnostics_are_stale():
    case = {"key": "PRI-0", "bulk": False, "split": "tune",
            "obligatory": [{"path": "src/core.py"}], "read_only": [{"path": "src/dep.py"}]}
    data = {"ranked_paths": ["src/core.py", "src/dep.py"],
            "context_paths": ["src/core.py", "src/dep.py"],
            "context": """// src/core.py#f (src/core.py:1-5)
1 | def f(): pass

// src/dep.py#g (src/dep.py:2-3)
2 | def g(): pass""",
            "elapsed_ms": 1, "context_chars": 100, "source_chars": 42}
    brief = {"brief": """- src/core.py:1 — видимая строка
- src/dep.py:3 — строка вне фрагмента
""", "paths": ["src/dep.py"], "invalid_references": [["src/core.py", 1]]}

    assert report.brief_paths(brief, data) == {"src/core.py"}
    score = report.add_brief_row(case, data, brief)
    assert (score["core_hits"], score["dependency_hits"]) == (1, 0)
    assert (score["path_only"]["core_hits"], score["path_only"]["dependency_hits"]) == (0, 1)
    assert score["path_only_losses"] == [{"path": "src/dep.py", "lines": [3], "kind": "read"}]
    assert score["citation_counts"] == {"verified": 1, "path_unseen": 0, "outside_excerpt": 1}


def test_mixed_brief_coverage_versions_cannot_be_paired():
    row = {"key": "PRI-0", "bulk": False, "core_recall": 0,
           "recall_at_10": 0, "ndcg_at_10": 0, "context_recall": 0,
           "context_chars": 0, "source_chars": 0, "elapsed_ms": 0}
    before = {**row, "brief_coverage_version": "path-only-v1"}
    after = {**row, "brief_coverage_version": "visible-lines-v2"}
    with pytest.raises(ValueError, match="coverage versions"):
        report.paired_comparison([before], [after])


def test_frozen_corpus_reproduces_pool_comparison():
    runs, batches, saved_briefs = report.load_evidence(pool_report.OUTPUT / "evidence.jsonl.gz")
    cases = report.read(replay.CORPUS)
    result, files = pool_report.score(cases, runs, saved_briefs, batches)
    historical = report.read(pool_report.OUTPUT / "results.json")

    expected_code = historical["comparisons"]["code"]
    # log2 and float summation can differ at the last bit across platforms and Python versions.
    for comparison in expected_code.values():
        for side in ("before", "after"):
            scores = comparison[side]
            scores["median_ndcg_at_10"] = pytest.approx(scores["median_ndcg_at_10"], rel=0, abs=1e-15)
    assert result["comparisons"]["code"] == expected_code
    assert result["brief_coverage_version"] == "visible-lines-v2"
    paired = result["comparisons"]["brief"]["all"]
    assert paired["n"] == 20
    assert paired["brief_coverage_version"] == "visible-lines-v2"
    assert paired["before"]["core_hits"] == paired["before"]["path_only"]["core_hits"] == 57
    assert (paired["after"]["core_hits"], paired["after"]["path_only"]["core_hits"]) == (51, 52)
    assert paired["before"]["dependency_hits"] == paired["after"]["dependency_hits"] == 17
    assert [paired[side]["path_only"]["median_core_recall"] for side in ("before", "after")] == [
        historical["comparisons"]["brief"]["all"][side]["median_core_recall"]
        for side in ("before", "after")
    ]
    assert result["by_arm"]["all-pool-30"]["citation_counts"] == {
        "verified": 110, "path_unseen": 25, "outside_excerpt": 8,
    }
    assert result["by_arm"]["all-pool-60"]["citation_counts"] == {
        "verified": 132, "path_unseen": 18, "outside_excerpt": 9,
    }

    assert result["comparisons"]["brief"]["bulk"]["n"] == 5
    assert len(files) == 390


def test_pool_path_only_replay_excludes_rejected_paths_without_updating_the_archive_row():
    runs, batches, saved_briefs = report.load_evidence(pool_report.OUTPUT / "evidence.jsonl.gz")
    cases = report.read(replay.CORPUS)
    brief = saved_briefs["all-pool-30"]["PRI-196"]
    rejected_path = "reviewer/entrypoints/mcp_server.py"
    assert rejected_path in runs["all-pool-30"]["PRI-196"]["modes"]["hybrid"]["context_paths"]
    assert rejected_path not in brief["paths"]
    brief["paths"].append(rejected_path)
    brief["brief"] += f"\n- {rejected_path}:85 — Исключено: отклонённый путь"

    result, _ = pool_report.score(cases, runs, saved_briefs, batches)

    assert result["comparisons"]["brief"]["all"]["before"]["path_only"]["core_hits"] == 57
    assert rejected_path in brief["paths"]


@pytest.mark.parametrize(
    ("module", "archive", "task_keys", "core_counts", "dep_hits", "losses"),
    [
        ("report", "evidence.jsonl.gz", ("luna",), (49, 53), 18, {
            ("PRI-196", "reviewer/entrypoints/mcp_server.py", (85,)),
            ("PRI-215", "reviewer/policy/policy.py", (16,)),
            ("PRI-228", "reviewer/install.py", (319,)),
            ("PRI-251", "reviewer/graph/store.py", (171,)),
        }),
        ("independent_summary", "independent/evidence.jsonl.gz", ("all-30", "brief"), (38, 41), 10, {
            ("PRI-179", "reviewer/graph/store.py", (76,)),
            ("PRI-213", "reviewer/tasks/boards/youtrack.py", (491,)),
            ("PRI-221", "reviewer/entrypoints/cli.py", (189,)),
        }),
    ],
)
def test_frozen_baseline_archives_reproduce_both_coverage_versions(
    tmp_path, module, archive, task_keys, core_counts, dep_hits, losses
):
    evidence = tmp_path / "copied-replay.jsonl.gz"
    copyfile(report.HERE / archive, evidence)
    completed = subprocess.run([
        sys.executable, "-m", f"eval.pri278.{module}", "--evidence", str(evidence),
        "--output", str(tmp_path),
    ], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr

    result = report.read(tmp_path / "results.json")
    assert result["brief_coverage_version"] == "visible-lines-v2"
    rows = result["per_task"]
    for key in task_keys:
        rows = rows[key]
    assert len(rows) == (20 if module == "report" else 10)
    assert {row["path_only"]["brief_coverage_version"] for row in rows} == {"path-only-v1"}
    assert (sum(row["core_hits"] for row in rows), sum(row["path_only"]["core_hits"] for row in rows)
            ) == core_counts
    assert sum(row["dependency_hits"] for row in rows) == dep_hits
    assert sum(row["path_only"]["dependency_hits"] for row in rows) == dep_hits
    assert {(row["key"], loss["path"], tuple(loss["lines"]))
            for row in rows for loss in row["path_only_losses"] if loss.get("kind") == "core"
            } == losses
