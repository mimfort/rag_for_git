"""Measure grounded Luna briefs on fixed-path symbol excerpts."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from eval.pri278 import briefs, report
from reviewer.mcp.brief_evidence import _visible_lines

HERE = Path(__file__).parent
LABELS = ("all-symbol-0", "all-symbol-1")


def load_baseline(source, field, runs):
    with gzip.open(HERE / source, "rt", encoding="utf-8") as evidence:
        return {
            row["brief"]["key"]: row
            for row in map(json.loads, evidence)
            if row.get(field) in runs and row.get("brief")
        }


def aggregate(cases, baseline, model, work):
    before_scores, after_scores, results = [], [], []
    for case in cases:
        key = case["key"]
        saved = baseline[key]
        cached_context = saved["retrieval"]["modes"]["hybrid"]
        if key in model[LABELS[0]]:
            arms = [
                report.read(work / label / f"{key}.json")["modes"]["hybrid"] for label in LABELS
            ]
            outputs = [model[label][key]["brief"] for label in LABELS]
            if arms[0]["context"] != cached_context["context"]:
                raise ValueError(f"Changed control for {key}")
        else:
            arms = (cached_context, cached_context)
            outputs = (saved["brief"]["brief"], saved["brief"]["brief"])
        checked = [
            briefs.check_references(text, arm["context"])
            for text, arm in zip(outputs, arms, strict=True)
        ]
        first, second = [
            report.add_citation_row(case, arm, citations)
            for arm, citations in zip(arms, checked, strict=True)
        ]
        pairs = [
            [(item["path"], item["line"], item["reason"]) for item in citations
             if item["status"] == "verified"]
            for citations in checked
        ]
        before_scores.append(first)
        after_scores.append(second)
        before = _visible_lines(arms[0]["context"])
        results.append({
            "key": key, "before": pairs[0], "after": pairs[1],
            "new_rows_cited": [(path, line, role) for path, line, role in pairs[1]
                              if line not in before.get(path, set())],
        })
    return before_scores, after_scores, results


def non_regression(counts):
    return (counts["core_delta_mean"] >= -1e-9 and
            counts["after"]["median_core_recall"] >= counts["before"]["median_core_recall"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    groups = (
        ("original", "corpus.json", "evidence.jsonl.gz", "run", report.BASELINE),
        ("independent", "independent-corpus.json", "independent/evidence.jsonl.gz",
         "variant", {"all-30"}),
    )
    batches = {label: report.model_batches(work, label) for label in LABELS}
    model = {label: report.model_index(items) for label, items in batches.items()}
    gate = {row["key"] for row in report.read(work / "corpus.json")}
    if any(set(values) != gate for values in model.values()):
        raise ValueError("Incomplete paired briefs")
    output = {"brief_coverage_version": briefs.COVERAGE_VERSION}
    before_all, after_all = [], []
    for name, corpus, file, field, runs in groups:
        cases = report.read(HERE / corpus)
        baseline = load_baseline(file, field, runs)
        before, after, citations = aggregate(cases, baseline, model, work)
        before_all.extend(before)
        after_all.extend(after)
        output[name] = {"metrics": report.paired_comparison(before, after), "citations": citations}
    output["combined"] = report.paired_comparison(before_all, after_all)
    output["coverage_non_regression"] = all(non_regression(counts) for counts in (
        output["original"]["metrics"]["all"], output["independent"]["metrics"]["all"],
        output["combined"]["bulk"],
    ))
    usage = [batch["usage"] for items in batches.values() for batch in items]
    output["usage"] = {key: sum(row[key] for row in usage) for key in usage[0]}
    for name, obj in (("luna.json", batches), ("brief_results.json", output)):
        (work / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
