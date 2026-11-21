"""Aggregate the newly labeled corpus and save the exact paired Luna prompts and results."""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
from pathlib import Path

from eval.pri278 import briefs as model_briefs
from eval.pri278 import report

HERE = Path(__file__).parent
LABELS = ("all-30", "all-deps-1", "all-tail-0", "all-tail-3")


def compare(left, right):
    output = report.paired_comparison(left, right)
    left_by_key = {row["key"]: row for row in left}
    for group in report.GROUPS:
        pairs = [(left_by_key[row["key"]], row) for row in report.group_rows(right, group)]
        output[group]["context_delta_mean"] = statistics.mean(
            b["context_recall"] - a["context_recall"] for a, b in pairs
        )
    return output


def meets_gate(comparison):
    for group in ("all", "bulk"):
        counts = comparison[group]
        if (counts["core_delta_mean"] < -1e-9 or
                counts["after"]["median_core_recall"] < counts["before"]["median_core_recall"]):
            return False
    counts = comparison["all"]
    return (counts["context_delta_mean"] > 1e-9 and
            counts["after"]["median_context_recall"] > counts["before"]["median_context_recall"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--work", type=Path)
    source.add_argument("--evidence", type=Path)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--tail-calibration", type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "independent")
    args = parser.parse_args()
    if args.work and not (args.calibration and args.tail_calibration):
        parser.error("--work requires --calibration and --tail-calibration")
    cases = report.read(HERE / "independent-corpus.json")
    if args.evidence:
        retrieval, batches, archived_briefs = report.load_evidence(
            args.evidence, run_field="variant", batch_field="model_batch"
        )
    else:
        retrieval = {label: report.read_run(args.work, label) for label in LABELS}
        batches = {label: report.model_batches(args.work, label) for label in LABELS}
        archived_briefs = None
    briefs = {label: report.model_index(items) for label, items in batches.items()}
    changed = set(briefs["all-tail-3"])
    if changed != set(briefs["all-tail-0"]):
        raise ValueError("Missing paired Luna brief")
    scores = {label: {"code": [], "brief": []} for label in LABELS}
    evidence = []
    for case in cases:
        key = case["key"]
        control = retrieval["all-30"][key]["modes"]["hybrid"]
        tail = retrieval["all-tail-3"][key]["modes"]["hybrid"]
        if (control["context"] != tail["context"]) != (key in changed):
            raise ValueError(f"Luna reuse did not match the actual input change for {key}")
        for label in LABELS:
            data = retrieval[label][key]["modes"]["hybrid"]
            reuse = label.startswith("all-tail-") and key not in changed
            if reuse and data["context"] != control["context"]:
                raise ValueError(f"Reused Luna brief has different context for {key} @ {label}")
            brief = briefs["all-30" if reuse else label][key]
            if archived_briefs is not None:
                saved = archived_briefs[label][key]
                if saved["brief"] != brief["brief"]:
                    raise ValueError(f"Reused Luna brief differs for {key} @ {label}")
                brief = saved
            scores[label]["code"].append(report.add_mode_row(case, data, data["context_paths"]))
            scores[label]["brief"].append(report.add_brief_row(case, data, brief))
            evidence.append({"variant": label, "key": key, "retrieval": retrieval[label][key],
                             "brief": brief, "identical_input_reused": reuse})
    paired = {
        mechanism: {stage: compare(scores[control][stage], scores[trial][stage])
                    for stage in ("code", "brief")}
        for mechanism, control, trial in (
            ("promote_one", "all-30", "all-deps-1"),
            ("fill_spare", "all-tail-0", "all-tail-3"),
        )
    }
    stats = {
        "brief_coverage_version": model_briefs.COVERAGE_VERSION,
        "independent": paired,
        "acceptance": {key: all(meets_gate(stage) for stage in value.values())
                       for key, value in paired.items()},
        "tail_changed_contexts": sorted(changed),
        "per_task": scores,
    }
    for name, path in (("calibration_code", args.calibration),
                       ("tail_calibration_code", args.tail_calibration)):
        if path:
            stats[name] = report.read(path)["comparisons"]
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    if args.evidence:
        return
    with gzip.open(args.output / "evidence.jsonl.gz", "wt", encoding="utf-8") as file:
        for row in evidence:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
        for label, items in batches.items():
            for item in items:
                meta = {key: value for key, value in item.items() if key != "briefs"}
                meta["keys"] = [brief["key"] for brief in item["briefs"]]
                file.write(json.dumps({"variant": label, "model_batch": meta}) + "\n")


if __name__ == "__main__":
    main()
