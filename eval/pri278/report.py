"""Aggregate the staged replay and export the first lost stage for each file."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from eval.pri278 import briefs as model_briefs
from eval.pri278.replay import K, MODES, PIPELINE_SHA

HERE = Path(__file__).parent
BASELINE = ("tune-30", "holdout-30")
INTERVENTION = ("tune-30-prod", "holdout-30-prod")
GROUPS = ("all", "ordinary", "bulk")
PATH_ONLY_VERSION = "path-only-v1"


def read(path):
    return json.loads(path.read_text())


def labels(case):
    return tuple({item["path"] for item in case[name]} for name in ("obligatory", "read_only"))


def ratio(paths, relevant):
    return len(set(paths) & relevant) / len(relevant)


def metrics(row, core, reads, paths):
    relevant = core | reads
    top = row["ranked_paths"][:K]
    dcg = sum(1 / math.log2(i + 2) for i, path in enumerate(top) if path in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(K, len(relevant))))
    return {
        "recall_at_10": ratio(top, relevant), "ndcg_at_10": dcg / ideal,
        "core_recall": ratio(paths, core), "context_recall": ratio(paths, reads),
        "elapsed_ms": row["elapsed_ms"], "context_chars": row["context_chars"],
        "source_chars": row["source_chars"],
    }


def percentile(values, p):
    return sorted(values)[math.ceil(len(values) * p) - 1]


def group_rows(rows, group):
    if group == "all":
        return rows
    return [row for row in rows if row["bulk"] == (group == "bulk")]


def summarize(rows):
    if not rows:
        return {"n": 0}
    summary = {
        "n": len(rows),
        **{f"median_{metric}": statistics.median(row[metric] for row in rows)
           for metric in ("recall_at_10", "ndcg_at_10", "core_recall", "context_recall",
                          "context_chars", "source_chars")},
        "p95_ms": percentile([row["elapsed_ms"] for row in rows], .95),
    }
    if version := rows[0].get("brief_coverage_version"):
        summary.update(
            brief_coverage_version=version,
            core_hits=sum(row["core_hits"] for row in rows),
            dependency_hits=sum(row["dependency_hits"] for row in rows),
            citation_counts=sum_citation_counts(rows),
        )
        if all("path_only" in row for row in rows):
            historical = [row["path_only"] for row in rows]
            summary["path_only"] = {
                "brief_coverage_version": PATH_ONLY_VERSION,
                "core_hits": sum(row["core_hits"] for row in historical),
                "dependency_hits": sum(row["dependency_hits"] for row in historical),
                "median_core_recall": statistics.median(row["core_recall"] for row in historical),
                "median_context_recall": statistics.median(row["context_recall"] for row in historical),
            }
    return summary


def sum_citation_counts(rows):
    return {
        status: sum(row["citation_counts"][status] for row in rows)
        for status in model_briefs.CITATION_STATUSES
    }


def minima(trace, name):
    ranks = {}
    for query in trace:
        for node, rank, _ in query[name]:
            path = node.split("#", 1)[0]
            ranks[path] = min(ranks.get(path, rank), rank)
    return ranks


def stage_position(paths, path):
    return paths.index(path) + 1 if path in paths else None


def explain(case, row, brief):
    data = row["modes"]["hybrid"]
    cited = brief_paths(brief, data)
    path_only = historical_brief_paths(brief, data)
    ranks = {name: minima(row["traces"], name) for name in ("bm25", "ann")}
    fused = {node.split("#", 1)[0] for q in row["traces"] for node in q["fused"]}
    qualified = {node.split("#", 1)[0] for q in row["traces"] for node in q["qualified"]}
    rerank = minima([{"rerank": row["modes"].get("hybrid_rerank", {}).get("rerank", [])}], "rerank")
    for name, gold in zip(("core", "read"), labels(case), strict=True):
        for path in sorted(gold):
            if path in cited:
                loss = "kept"
            elif path in data["context_paths"]:
                loss = "brief"
            elif path in data["selected_paths"]:
                loss = "render_limit"
            elif path in data["expanded_paths"]:
                loss = "file_budget"
            elif path in qualified:
                raise ValueError(f"Qualified gold path vanished from ranked/expanded candidates: {path}")
            elif path in fused:
                loss = "ann_distance"
            elif path in ranks["bm25"] or path in ranks["ann"]:
                loss = "fusion_cap"
            elif path in row["index_paths"]:
                loss = "initial_pool"
            else:
                loss = "not_indexed"
            yield {
                "key": case["key"], "split": case["split"], "bulk": case["bulk"],
                "kind": name, "path": path, "first_loss": loss,
                "bm25_rank": ranks["bm25"].get(path), "ann_rank": ranks["ann"].get(path),
                "fused": path in fused, "qualified": path in qualified,
                "hybrid_file_rank": stage_position(data["ranked_paths"], path),
                "rerank_chunk_rank": rerank.get(path),
                "graph_rescue": path in data["expanded_paths"] and path not in qualified,
                "index": path in row["index_paths"], "in_file_budget": path in data["selected_paths"],
                "in_context": path in data["context_paths"], "in_luna_brief": path in cited,
                "in_path_only_brief": path in path_only,
                "in_rerank_context": path in row["modes"].get("hybrid_rerank", {}).get("context_paths", []),
            }


def model_batches(work, label):
    return [read(path) for path in sorted((work / f"luna-{label}").glob("batch-*.json"))]


def model_index(batches):
    return {row["key"]: row for batch in batches for row in batch["briefs"]}


def brief_paths(brief, data):
    return model_briefs.verified_paths(model_briefs.check_references(brief["brief"], data["context"]))


def historical_brief_paths(brief, data):
    return set(brief["paths"]) & set(data["context_paths"])


def add_mode_row(case, data, paths):
    return {
        "key": case["key"], "bulk": case["bulk"], "split": case["split"],
        **metrics(data, *labels(case), paths),
    }


def add_citation_row(case, data, citations, *, historical_paths=None):
    paths = model_briefs.verified_paths(citations)
    core, reads = labels(case)
    row = {
        **add_mode_row(case, data, paths),
        "brief_coverage_version": model_briefs.COVERAGE_VERSION,
        "core_hits": len(core & paths), "dependency_hits": len(reads & paths),
        "citation_counts": model_briefs.citation_counts(citations),
    }
    if historical_paths is not None:
        row["path_only"] = {
            "brief_coverage_version": PATH_ONLY_VERSION,
            "core_hits": len(core & historical_paths),
            "dependency_hits": len(reads & historical_paths),
            "core_recall": ratio(historical_paths, core),
            "context_recall": ratio(historical_paths, reads),
        }
        kinds = {**dict.fromkeys(reads, "read"), **dict.fromkeys(core, "core")}
        row["path_only_losses"] = [
            {
                "path": path,
                "lines": sorted(item["line"] for item in citations if item["path"] == path),
                "kind": kinds.get(path, "unlabeled"),
            }
            for path in sorted(historical_paths - paths)
        ]
    return row


def add_brief_row(case, data, brief):
    citations = model_briefs.check_references(brief["brief"], data["context"])
    return add_citation_row(case, data, citations, historical_paths=historical_brief_paths(brief, data))


def arm_comparison(arms):
    result = {}
    for name, rows in arms.items():
        result[name] = {group: summarize(group_rows(rows, group)) for group in GROUPS}
    return result


def paired_comparison(before, after):
    previous = {row["key"]: row for row in before}
    pairs = [(previous[row["key"]], row) for row in after]
    versions = {row.get("brief_coverage_version") for pair in pairs for row in pair}
    if len(versions) > 1:
        raise ValueError("Cannot compare different brief coverage versions")
    version, = versions or {None}
    result = {}
    for group in GROUPS:
        selected = [pair for pair in pairs if group == "all" or pair[0]["bulk"] == (group == "bulk")]
        deltas = [b["core_recall"] - a["core_recall"] for a, b in selected]
        result[group] = {
            "n": len(selected),
            "before": summarize([a for a, _ in selected]),
            "after": summarize([b for _, b in selected]),
            "core_delta_median": statistics.median(deltas) if deltas else None,
            "core_delta_mean": statistics.mean(deltas) if deltas else None,
            "core_up": sum(d > 1e-9 for d in deltas),
            "core_down": sum(d < -1e-9 for d in deltas),
        }
        if version:
            result[group]["brief_coverage_version"] = version
    return result


def read_run(work, label):
    return {path.stem: read(path) for path in (work / label).glob("PRI-*.json")}


def load_evidence(path, *, run_field="run", batch_field="batch"):
    runs, batches, briefs = defaultdict(dict), defaultdict(list), defaultdict(dict)
    with gzip.open(path, "rt", encoding="utf-8") as file:
        for line in file:
            row = json.loads(line)
            label = row[run_field]
            if batch_field in row:
                batches[label].append(row[batch_field])
                continue
            retrieval = row["retrieval"]
            key = row["key"] if "key" in row else retrieval["key"]
            runs[label][key] = retrieval
            if row["brief"] is not None:
                briefs[label][key] = row["brief"]
    for label, captured_batches in batches.items():
        for batch in captured_batches:
            batch["briefs"] = [briefs[label][key] for key in batch.pop("keys")]
    return runs, batches, briefs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--work", type=Path)
    source.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path, default=HERE)
    args = parser.parse_args()
    cases = read(HERE / "corpus.json")
    if args.evidence:
        runs, batches, briefs = load_evidence(args.evidence)
    else:
        runs = {label: read_run(args.work, label) for label in (*BASELINE, *INTERVENTION)}
        batches = {label: model_batches(args.work, label) for label in (*BASELINE, *INTERVENTION)}
        briefs = {label: model_index(rows) for label, rows in batches.items()}
    stage_rows = []
    arms = {mode: [] for mode in (*MODES, "luna")}
    summary = {"pipeline_sha": PIPELINE_SHA, "brief_coverage_version": model_briefs.COVERAGE_VERSION,
               "failures": [], "usage": {},
               "comparisons": {}, "per_task": {}}
    for case in cases:
        key, split = case["key"], case["split"]
        label = f"{split}-30"
        row, brief = runs[label][key], briefs[label][key]
        stage_rows.extend(explain(case, row, brief))
        summary["failures"].extend([key, mode, detail] for mode, detail in row["failures"].items())
        for mode, data in row["modes"].items():
            arms[mode].append(add_mode_row(case, data, data["context_paths"]))
        data = row["modes"]["hybrid"]
        arms["luna"].append(add_brief_row(case, data, brief))
    summary["arms"] = arm_comparison(arms)
    summary["per_task"] = arms
    summary["rerank_vs_hybrid_code"] = paired_comparison(arms["hybrid"], arms["hybrid_rerank"])
    for key in ("all", "bulk", "ordinary", "core", "read"):
        summary.setdefault("losses", {})[key] = dict(Counter(
            row["first_loss"] for row in stage_rows
            if key == "all" or row["kind"] == key
            or ("bulk" if row["bulk"] else "ordinary") == key
        ))
    for split in ("tune", "holdout"):
        control_label, treatment_label = f"{split}-30", f"{split}-30-prod"
        control_code, treatment_code, control_brief, treatment_brief = [], [], [], []
        for case in cases:
            key = case["key"]
            if case["split"] != split or key not in runs[treatment_label]:
                continue
            control = runs[control_label][key]["modes"]["hybrid"]
            treatment = runs[treatment_label][key]["modes"]["hybrid"]
            control_code.append(add_mode_row(case, control, control["context_paths"]))
            treatment_code.append(add_mode_row(case, treatment, treatment["context_paths"]))
            if key in briefs[treatment_label]:
                b0, b1 = briefs[control_label][key], briefs[treatment_label][key]
                control_brief.append(add_brief_row(case, control, b0))
                treatment_brief.append(add_brief_row(case, treatment, b1))
        summary["comparisons"][split] = {
            "code": paired_comparison(control_code, treatment_code),
            "brief": paired_comparison(control_brief, treatment_brief),
            "per_task": {"before": control_code, "after": treatment_code,
                         "brief_before": control_brief, "brief_after": treatment_brief},
        }
    for label in (*BASELINE, *INTERVENTION):
        rows = batches[label]
        citations = (
            citation
            for key, brief in briefs[label].items()
            for citation in model_briefs.check_references(
                brief["brief"], runs[label][key]["modes"]["hybrid"]["context"]
            )
        )
        summary["usage"][label] = {
            "batches": len(rows),
            "invalid_references": sum(len(b["invalid_references"]) for row in rows for b in row["briefs"]),
            "citation_counts": model_briefs.citation_counts(citations),
            "batch_elapsed_s": [row["batch_elapsed_s"] for row in rows],
            "tokens": dict(sum((Counter(row["usage"]) for row in rows), Counter())),
        }
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "per_file.tsv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=stage_rows[0].keys(), delimiter="\t")
        writer.writeheader()
        writer.writerows(stage_rows)
    (args.output / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    if args.evidence:
        return
    with (args.output / "evidence.jsonl.gz").open("wb") as file:
        with gzip.GzipFile(fileobj=file, mode="wb", filename="", mtime=0) as archive:
            for label, rows in runs.items():
                for row in sorted(rows.values(), key=lambda item: item["key"]):
                    enriched = {"run": label, "retrieval": row, "brief": briefs[label].get(row["key"])}
                    archive.write((json.dumps(enriched, ensure_ascii=False) + "\n").encode("utf-8"))
            for label, rows in batches.items():
                for row in rows:
                    info = {key: row[key] for key in row if key != "briefs"}
                    info["keys"] = [item["key"] for item in row["briefs"]]
                    archive.write((json.dumps({"run": label, "batch": info}) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
