"""Check that the identical-input comparison survives replay without model calls."""

from eval.pri278 import append_probe, brief_variance, report


def test_frozen_repeated_luna_briefs_reproduce_the_reference_control():
    result = brief_variance.analyze()
    expected = report.read(brief_variance.HERE / "brief_variance/results.json")

    assert result["brief_coverage_version"] == "visible-lines-v2"
    assert result["original"]["paired"]["n"] == 20
    assert result["independent"]["paired"]["n"] == 8
    assert {key: result["combined"][key] for key in expected["combined"]} == expected["combined"]
    for subset in ("original", "independent"):
        for old, new in zip(expected[subset]["per_task"], result[subset]["per_task"], strict=True):
            assert {key: new[key] for key in old} == old


def test_append_scores_each_brief_against_the_context_seen_when_written():
    base_context = """// src/base.py#f (src/base.py:1-1)
1 | def f(): pass

// src/hidden.py#g (src/hidden.py:10-11)
10 | def g(): pass"""
    append_context = """// src/wrong_extra.py#g (src/wrong_extra.py:4-5)
4 | def g(): pass

// src/valid_extra.py#h (src/valid_extra.py:7-7)
7 | def h(): pass"""
    expanded_context = base_context + "\n11 | return None\n\n" + append_context.replace(
        "4 | def g(): pass", "4 | def g(): pass\n5 | return None",
    )
    base_paths = ["src/base.py", "src/hidden.py"]
    extra_paths = ["src/wrong_extra.py", "src/valid_extra.py"]

    def retrieval(paths, context):
        return {"modes": {"hybrid": {
            "context": context, "context_paths": paths, "ranked_paths": paths,
            "context_chars": len(context), "source_chars": len(context), "elapsed_ms": 1,
        }}}

    cases, batch, additions = [], [], []
    control, expanded, baseline = {}, {}, {}
    for key, bulk in (("PRI-1", False), ("PRI-2", True)):
        cases.append({"key": key, "bulk": bulk, "split": "holdout",
                      "obligatory": [{"path": path} for path in base_paths],
                      "read_only": [{"path": path} for path in extra_paths]})
        batch.append({"key": key, "context": append_context})
        additions.append({"key": key, "brief": """- src/wrong_extra.py:5 — строка вне дополнения
- src/valid_extra.py:7 — видимая строка дополнения
- src/base.py:1 — путь отсутствует в дополнении"""})
        baseline[key] = {"paths": base_paths, "brief": """- src/base.py:1 — видимая строка
- src/hidden.py:11 — строка вне исходной секции
- src/valid_extra.py:7 — путь отсутствует в исходной секции"""}
        control[key] = retrieval(base_paths, base_context)
        expanded[key] = retrieval(base_paths + extra_paths, expanded_context)

    result = append_probe.summarize(batch, additions, cases, control, expanded, baseline)

    paired = result["comparisons"]["all"]
    assert paired["brief_coverage_version"] == "visible-lines-v2"
    assert paired["before"]["median_core_recall"] == paired["after"]["median_core_recall"] == .5
    assert paired["after"]["median_context_recall"] == .5
    assert paired["after"]["citation_counts"] == {
        "verified": 4, "path_unseen": 0, "outside_excerpt": 4,
    }
    historical_before, historical_after = (paired[side]["path_only"] for side in ("before", "after"))
    assert historical_before["median_core_recall"] == historical_after["median_core_recall"] == 1.
    assert historical_before["dependency_hits"] == 0
    assert historical_after["dependency_hits"] == 2
    assert result["additions"][0]["accepted"] == ["src/valid_extra.py"]
    assert [proposal["status"] for proposal in result["additions"][0]["proposals"]] == [
        "outside_excerpt", "verified", "path_unseen",
    ]
