"""Сверить ссылки архивных брифов PRI-278 с двумя последующими Git-снимками."""
from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from time import perf_counter

from eval.pri278.briefs import check_references
from reviewer.mcp.brief_evidence import _HEADER
from reviewer.services.brief_provenance import GitCheckout, compare_citations

_ARCHIVE = Path(__file__).resolve().parents[1] / "pri278"
_ROW = re.compile(r"^\s*(\d+) \| (.*)$")
_WINDOW_SECONDS = 7 * 24 * 60 * 60


def archive_rows(archive: Path, allowed: set[str]):
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("run", record.get("variant")) not in allowed:
                continue
            retrieval = record.get("retrieval")
            brief = record.get("brief")
            if retrieval and brief and "hybrid" in retrieval.get("modes", {}):
                yield retrieval, brief["brief"]


def snippet_rows(context: str) -> dict[tuple[str, int], set[bytes]]:
    found = defaultdict(set)
    path = None
    for row in context.split("\n"):
        if row == "[...truncated]":
            break
        if heading := _HEADER.fullmatch(row):
            path = heading["path"]
        elif path and (match := _ROW.fullmatch(row)):
            found[path, int(match[1])].add(match[2].encode("utf-8"))
    return found


def snapshot_times(git: GitCheckout, sources: set[str]) -> dict[str, int]:
    return {
        sha: int(timestamp)
        for sha in sorted(sources)
        if (timestamp := git.git("show", "-s", "--format=%ct", sha)) is not None
    }


def nearest_commit(git: GitCheckout, source: str, snapshots: dict[str, int]) -> str | None:
    if source not in snapshots:
        return None
    start = snapshots[source]
    ordered = sorted(snapshots.items(), key=lambda entry: (entry[1], entry[0]))
    return next(
        (
            sha for sha, timestamp in ordered
            if sha != source
            and 0 <= timestamp - start <= _WINDOW_SECONDS
            and git.git("merge-base", "--is-ancestor", source, sha) is not None
        ),
        None,
    )


def prepare_case(git: GitCheckout, source: str, brief: str, context: str):
    visible = snippet_rows(context)
    selected, blobs = [], {}
    exclusions = Counter()
    for citation in check_references(brief, context):
        if citation["status"] != "verified":
            exclusions[citation["status"]] += 1
            continue
        path, line = citation["path"], citation["line"]
        blob = git.blob(source, path)
        lines = git.lines(blob) if blob else None
        if lines is None or line > len(lines):
            exclusions["source_unavailable"] += 1
        elif visible[path, line] != {lines[line - 1]}:
            exclusions["source_text_mismatch"] += 1
        else:
            selected.append(("code", path, line))
            blobs[path] = blob
    return selected, blobs, exclusions


def replay_case(root: str, source: str, target: str, anchors, blobs):
    # Для времени проверки не используем Git-кэш, заполненный разметкой эксперимента.
    git = GitCheckout(root)
    started = perf_counter()
    verdicts = compare_citations(git, anchors, blobs, source, target, set())
    elapsed_ms = (perf_counter() - started) * 1000

    for result in verdicts:
        path, line = result["path"], result["line"]
        source_text = git.git("show", f"{source}:{path}")
        target_text = git.git("show", f"{target}:{path}")
        assert source_text is not None
        original = source_text.splitlines()[line - 1]
        other = target_text.splitlines() if target_text is not None else []
        observed_drift = line > len(other) or original != other[line - 1]
        detected_drift = result["status"] in {"changed", "moved", "ambiguous", "path_missing"}
        suggested = result.get("suggested_line")
        result["observed_drift"] = observed_drift
        result["detected_drift"] = detected_drift
        if suggested is not None:
            result["correct_suggestion"] = (other[suggested - 1] == original
                                            and other.count(original) == 1)
    return {"target_sha": target, "elapsed_ms": round(elapsed_ms, 3), "citations": verdicts}


def summarize(cases: dict, arm: str) -> dict:
    pairs = [row[arm] for row in cases.values() if row[arm]]
    citations = [citation for row in pairs for citation in row["citations"]]
    return {
        "tasks": len(pairs),
        "citations": len(citations),
        "statuses": dict(Counter(row["status"] for row in citations)),
        "drift": sum(row["observed_drift"] for row in citations),
        "missed_drift": sum(row["observed_drift"] and not row["detected_drift"]
                            for row in citations),
        "false_drift": sum(not row["observed_drift"] and row["detected_drift"]
                           for row in citations),
        "correct_suggestions": sum(row.get("correct_suggestion", False) for row in citations),
        "median_ms": round(median(row["elapsed_ms"] for row in pairs), 3) if pairs else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    corpora = ("corpus.json", "independent-corpus.json")
    solutions = {row["key"]: row["solution_sha"] for filename in corpora
                 for row in json.loads((_ARCHIVE / filename).read_text())}
    baselines = (
        (_ARCHIVE / "evidence.jsonl.gz", {"tune-30", "holdout-30"}),
        (_ARCHIVE / "independent" / "evidence.jsonl.gz", {"all-30"}),
    )
    git = GitCheckout(args.repo)
    archive_cases = [(retrieval, brief) for archive, allowed in baselines
                     for retrieval, brief in archive_rows(archive, allowed)]
    snapshots = snapshot_times(git, {retrieval["base_sha"] for retrieval, _ in archive_cases})
    results = {}
    exclusions = Counter()
    for retrieval, brief in archive_cases:
        key, source = retrieval["key"], retrieval["base_sha"]
        assert key not in results
        code = retrieval["modes"]["hybrid"]["context"]
        anchors, blobs, skipped = prepare_case(git, source, brief, code)
        exclusions.update(skipped)
        targets = {
            "within_week": nearest_commit(git, source, snapshots),
            "after_implementation": git.commit(solutions[key]),
        }
        result = {"source_sha": source, "eligible_citations": len(anchors)}
        for arm, target in targets.items():
            result[arm] = replay_case(args.repo, source, target, anchors, blobs) if target and anchors else None
            if target is None:
                exclusions[f"{arm}_no_target"] += len(anchors)
        results[key] = result
    output = {
        "tasks_total": len(results), "excluded": dict(exclusions),
        "within_week": summarize(results, "within_week"),
        "after_implementation": summarize(results, "after_implementation"),
        "tasks": results,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: value for key, value in output.items() if key != "tasks"}, indent=2))


if __name__ == "__main__":
    main()
