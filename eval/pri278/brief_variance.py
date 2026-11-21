"""Compare independently generated Luna briefs for byte-identical task inputs."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import itertools
import json
import statistics
from collections import defaultdict
from pathlib import Path

from eval.pri278 import briefs, report

HERE = Path(__file__).parent


def archived_runs(path, field, runs):
    with gzip.open(HERE / path, "rt", encoding="utf-8") as evidence:
        for row in map(json.loads, evidence):
            if (row.get(field) not in runs or not row.get("brief")
                    or row.get("identical_input_reused")):
                continue
            context = row.get("context")
            if context is None:
                context = row["retrieval"]["modes"]["hybrid"]["context"]
            brief = row["brief"]
            yield row[field], brief["key"], context, brief["brief"]


def sources():
    runs = [
        ("evidence.jsonl.gz", "run", report.BASELINE),
        ("pool_size/evidence.jsonl.gz", "run", {"all-pool-30"}),
        ("pool_size/evidence.jsonl.gz", "run", {"all-pool-60"}),
        ("followup/graph_luna.jsonl.gz", "variant", {"holdout-rank-30-base-normal"}),
        ("independent/evidence.jsonl.gz", "variant", {"all-30"}),
        ("independent/evidence.jsonl.gz", "variant", {"all-tail-0"}),
        ("independent/evidence.jsonl.gz", "variant", {"all-deps-1"}),
        ("independent/evidence.jsonl.gz", "variant", {"all-tail-3"}),
    ]
    primary = {}
    for path, field, labels in runs:
        for label, key, context, text in archived_runs(path, field, labels):
            primary.setdefault(key, context)
            yield label, key, context, text
    symbols = report.model_index(report.read(HERE / "symbol_coverage/luna.json")["all-symbol-0"])
    for key, brief in symbols.items():
        # symbol_summary asserts its freshly generated control uses the primary
        # context byte for byte before writing the released archive.
        yield "all-symbol-0", key, primary[key], brief["brief"]


def compare(case, pair):
    (left, context, first), (right, _, second) = pair
    checked = [briefs.check_references(text, context) for text in (first, second)]
    core, dependencies = report.labels(case)
    a, b = (briefs.verified_paths(citations) for citations in checked)
    union = a | b
    return {
        "key": case["key"], "bulk": case["bulk"], "runs": [left, right],
        "input_sha256": hashlib.sha256(
            json.dumps([case["input"], context], ensure_ascii=False).encode()
        ).hexdigest(),
        "core_before": report.ratio(a, core), "core_after": report.ratio(b, core),
        "dependency_before": report.ratio(a, dependencies),
        "dependency_after": report.ratio(b, dependencies),
        "equal": a == b, "jaccard": len(a & b) / len(union) if union else 1.0,
        "paths_left": sorted(a - b), "paths_right": sorted(b - a),
        "brief_coverage_version": briefs.COVERAGE_VERSION,
        "citation_counts": {
            side: briefs.citation_counts(citations)
            for side, citations in zip(("before", "after"), checked, strict=True)
        },
    }


def summarize(pairs):
    if not pairs:
        return {"n": 0}
    changes = [abs(row["core_before"] - row["core_after"]) for row in pairs]
    dependency_changes = [abs(row["dependency_before"] - row["dependency_after"]) for row in pairs]
    return {
        "n": len(pairs), "same_files": sum(row["equal"] for row in pairs),
        "core_changed": sum(change > 1e-9 for change in changes),
        "abs_core_delta_median": statistics.median(changes),
        "abs_core_delta_p90": report.percentile(changes, .90),
        "abs_dependency_delta_median": statistics.median(dependency_changes),
        "jaccard_median": statistics.median(row["jaccard"] for row in pairs),
    }


def analyze():
    by_key = defaultdict(list)
    for label, key, context, text in sources():
        by_key[key].append((label, context, text))
    result = {"brief_coverage_version": briefs.COVERAGE_VERSION}
    all_pairs = []
    for name, path in (("original", "corpus.json"), ("independent", "independent-corpus.json")):
        cases = report.read(HERE / path)
        pairs = []
        for case in cases:
            for left, right in itertools.combinations(by_key[case["key"]], 2):
                if left[0] != right[0] and left[1] == right[1]:
                    pairs.append(compare(case, (left, right)))
                    break
        all_pairs.extend(pairs)
        result[name] = {"paired": summarize(pairs), "total": len(cases), "per_task": pairs}
    result["combined"] = summarize(all_pairs)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
