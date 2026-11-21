"""Report the paired 30/60 candidate-depth experiment, online or from evidence."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter
from pathlib import Path

from eval.pri278 import briefs as model_briefs
from eval.pri278 import replay, report

LABELS = ("all-pool-30", "all-pool-60")
OUTPUT = Path(__file__).with_name("pool_size")


def input_paths(trace):
    return {node.split("#", 1)[0] for source in ("bm25", "ann") for node, _, _ in trace[source]}


def local_times(row):
    # The embedding request is shared by both paired searches.
    return {name: round(sum(trace[f"{name}_ms"] for trace in row["traces"]), 3)
            for name in ("bm25", "ann")}


def verify_pair(case, baseline, treatment):
    for pool, run in zip((30, 60), (baseline, treatment), strict=True):
        if (run["base_sha"], run["candidate_pool"], run["merge_limit"], run["ann_ef_search"]) != (
            case["base_sha"], pool, 30, 60,
        ) or run["production_only"] or run["failures"]:
            raise ValueError(f"Unexpected replay configuration for {case['key']} @ {pool}")
        for query in run["traces"]:
            if len(query["ann"]) != pool or len(query["bm25"]) > pool or len(query["fused"]) > 30:
                raise ValueError(f"Unexpected candidate depth for {case['key']} @ {pool}")
        if len(run["modes"]["hybrid"]["context_paths"]) > replay.LIMITS.code_section.max_files:
            raise ValueError(f"File budget exceeded for {case['key']} @ {pool}")
    for shared in ("chunks", "index_paths", "graph_nodes", "graph_edges", "index_ms", "models"):
        if baseline[shared] != treatment[shared]:
            raise ValueError(f"Pair has different {shared} for {case['key']}")


def score(cases, runs, briefs, batches):
    pairs = {name: {"code": [], "brief": [], "search": []} for name in LABELS}
    files = []
    out_of_context = Counter()
    for case in cases:
        key = case["key"]
        verify_pair(case, *(runs[label][key] for label in LABELS))
        for label in LABELS:
            row = runs[label][key]
            data = row["modes"]["hybrid"]
            saved = briefs[label][key]
            # The pool's original CLI rebuilt its path-only input from the Markdown.
            paths_in_brief = {path for path, _, _ in model_briefs.parsed_references(saved["brief"])}
            brief = {**saved, "paths": paths_in_brief}
            out_of_context[label] += len(paths_in_brief - set(data["context_paths"]))
            code_score = report.add_mode_row(case, data, data["context_paths"])
            brief_score = report.add_brief_row(case, data, brief)
            pairs[label]["code"].append(code_score)
            pairs[label]["brief"].append(brief_score)
            full_gold = set.union(*report.labels(case))
            paths = set().union(*(input_paths(q) for q in row["traces"]))
            times = local_times(row)
            pairs[label]["search"].append({
                "key": key, "bulk": case["bulk"], "split": case["split"],
                "bm25": len(row["traces"][0]["bm25"]), "ann": len(row["traces"][0]["ann"]),
                "raw_relevant": len(paths & full_gold), "raw_recall": report.ratio(paths, full_gold),
                "local_ms": round(sum(times.values()), 3), **{f"{k}_ms": v for k, v in times.items()},
            })
            for file in report.explain(case, row, brief):
                files.append({"input_pool": row["candidate_pool"], **file})

    comparisons = {
        stage: report.paired_comparison(*(pairs[label][stage] for label in LABELS))
        for stage in ("code", "brief")
    }
    summaries = {}
    for label, data in pairs.items():
        search = data["search"]
        summaries[label] = {
            "first_loss": dict(Counter(row["first_loss"] for row in files
                                       if row["input_pool"] == runs[label][cases[0]["key"]]["candidate_pool"])),
            "raw_relevant": sum(row["raw_relevant"] for row in search),
            "local_p95_ms": report.percentile([r["local_ms"] for r in search], .95),
            "tokens": dict(sum((Counter(batch["usage"]) for batch in batches[label]), Counter())),
            "citation_counts": report.sum_citation_counts(data["brief"]),
            "out_of_context_citations": out_of_context[label],
        }
    counts = {"cases": len(cases), "bulk_cases": sum(case["bulk"] for case in cases)}
    return {"pipeline_sha": replay.PIPELINE_SHA, "brief_coverage_version": model_briefs.COVERAGE_VERSION,
            **counts, "comparisons": comparisons,
            "by_arm": summaries, "per_task": pairs}, files


def write_evidence(path, runs, briefs, batches):
    with path.open("wb") as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", filename="", mtime=0) as archive:
            def emit(row):
                archive.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))

            for label in LABELS:
                for key in sorted(runs[label]):
                    emit({"run": label, "retrieval": runs[label][key], "brief": briefs[label][key]})
                for batch in batches[label]:
                    meta = {key: row for key, row in batch.items() if key != "briefs"}
                    meta["keys"] = [row["key"] for row in batch["briefs"]]
                    emit({"run": label, "batch": meta})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--work", type=Path)
    source.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    cases = report.read(replay.CORPUS)
    if args.evidence:
        runs, batches, briefs = report.load_evidence(args.evidence)
    else:
        runs = {label: report.read_run(args.work, label) for label in LABELS}
        batches = {label: report.model_batches(args.work, label) for label in LABELS}
        briefs = {label: report.model_index(rows) for label, rows in batches.items()}
    results, files = score(cases, runs, briefs, batches)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    with (args.output / "per_file.tsv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=files[0], delimiter="\t")
        writer.writeheader()
        writer.writerows(files)
    if args.work:
        write_evidence(args.output / "evidence.jsonl.gz", runs, briefs, batches)


if __name__ == "__main__":
    main()
