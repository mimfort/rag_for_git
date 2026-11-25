"""Пересчитать парные сравнения инструкций для брифа на сохранённых ответах."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from eval.pri299.pilot import digest, index_response, load_conditions, score_structured
from eval.pri310.replay import MIN_LARGE_FILES, load_inputs, read_json
from reviewer.gitutil import file_at_ref
from reviewer.mcp.brief_anchors import check_brief_anchors

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CORPUS = REPO / "eval" / "pri278"
REF = "740f9e871e14010a05800153f7f930e0e34d2f60"
REPEATS = (1, 2)
CORPORA = ("PRI-278", "PRI-283")
COHORTS = ("large", "ordinary")
COUNT_FIELDS = (
    "core", "core_total", "dep", "dep_total", "paths", "labelled", "invalid", "git_rejects", "missing",
)


def make_batches() -> tuple[dict[tuple[str, str], list[dict]], dict]:
    main, labels = load_inputs()
    cases = [{**case, "corpus": "PRI-278", "context": case["contexts"]["rank"]} for case in main]
    with gzip.open(CORPUS / "independent" / "evidence.jsonl.gz", "rt", encoding="utf-8") as stream:
        archive = {
            row["key"]: row["retrieval"] for row in map(json.loads, stream)
            if row.get("variant") == "all-30" and "retrieval" in row
        }
    for case in read_json(CORPUS / "independent-corpus.json"):
        key, sha = case["key"], case["base_sha"]
        if key in labels or archive[key]["base_sha"] != sha:
            raise ValueError(f"Повтор ключа или несовпадение Git-снимка для {key}")
        cases.append({
            "key": key, "task": case["input"], "base_sha": sha, "corpus": "PRI-283",
            "context": archive[key]["modes"]["hybrid"]["context"],
        })
        labels[key] = {
            "core": {row["path"] for row in case["obligatory"]},
            "dependencies": {row["path"] for row in case["read_only"]},
        }
    batches = {}
    for corpus in CORPORA:
        members = [case for case in cases if case["corpus"] == corpus]
        members.sort(key=lambda case: int(case["key"].split("-")[1]))
        large = [case for case in members if len(labels[case["key"]]["core"]) >= MIN_LARGE_FILES]
        ordinary = [case for case in members if len(labels[case["key"]]["core"]) < MIN_LARGE_FILES]
        batches[corpus, "large"] = large
        batches[corpus, "ordinary"] = ordinary[:4]
    return batches, labels


def checked_sections(case: dict) -> dict[str, set[int]]:
    def read_sources(paths: list[str]) -> dict[str, str]:
        return {
            path: text for path in paths
            if (text := file_at_ref(str(REPO), path, case["base_sha"])) is not None
        }

    sections, diagnostics = check_brief_anchors({"code": case["context"]}, read_sources)
    counts = diagnostics.get("code", {})
    if not counts.get("checked") or counts["mismatched"] or counts["unavailable"]:
        raise ValueError(f"Архивные строки {case['key']} расходятся с Git: {counts}")
    return sections["code"]


def score_case(brief: dict | None, case: dict, gold: dict, allowed: dict[str, set[int]]) -> dict:
    verified, statuses, errors, ok = (
        score_structured(brief, case["context"])
        if brief is not None else ([], [], ["missing"], False)
    )
    paths = {path for path, line in verified if line in allowed.get(path, ())}
    git_rejects = sum(line not in allowed.get(path, ()) for path, line in verified)
    return {
        "key": case["key"],
        "core": len(paths & gold["core"]),
        "core_total": len(gold["core"]),
        "dep": len(paths & gold["dependencies"]),
        "dep_total": len(gold["dependencies"]),
        "paths": len(paths),
        "labelled": len(paths & (gold["core"] | gold["dependencies"])),
        "invalid": sum(status != "verified" for status in statuses) + git_rejects,
        "git_rejects": git_rejects,
        "missing": not ok,
        "errors": errors,
        "filed_paths": sorted(paths),
    }


def validate_protocol(protocol: dict, batches: dict, candidate: str) -> None:
    baseline, schema = load_conditions()["structured"]
    prompts = protocol["prompt"]
    if (
        protocol["base_sha"] != REF or set(prompts) != {"baseline", candidate}
        or prompts["baseline"] != baseline or protocol["schema_sha"] != digest(json.dumps(schema, ensure_ascii=False))
        or protocol["prompt_sha"] != {name: digest(prompt) for name, prompt in prompts.items()}
    ):
        raise ValueError("Изменился протокол сравнения")
    expected = {}
    for (corpus, size), batch in batches.items():
        context = [{field: case[field] for field in ("key", "task", "context")} for case in batch]
        expected[f"{corpus}-{size}"] = {
            "cases": [{field: case[field] for field in ("key", "base_sha")} for case in batch],
            "input_sha": digest(json.dumps(context, ensure_ascii=False)),
        }
    if protocol["batches"] != expected:
        raise ValueError("Изменилась разметка пакетов или текст входа")


def score_experiment(archive: dict, candidate: str, batches: dict, labels: dict, allowed: dict) -> list[dict]:
    validate_protocol(archive["protocol"], batches, candidate)
    measurements = []
    responses = archive["responses"]
    expected = {
        f"r{repeat}-{corpus}-{size}-{condition}"
        for corpus, size in batches for repeat in REPEATS for condition in ("baseline", candidate)
    }
    if responses.keys() != expected:
        raise ValueError("Архив содержит неполный набор ответов пакетов")
    for (corpus, size), batch in batches.items():
        batch_keys = {case["key"] for case in batch}
        for repeat in REPEATS:
            for condition in ("baseline", candidate):
                result = responses[f"r{repeat}-{corpus}-{size}-{condition}"]
                if result["condition"] != condition or result["rep"] != repeat:
                    raise ValueError("Ответ помечен другим условием")
                indexed = index_response(result["response"])
                if set(indexed) - batch_keys:
                    raise ValueError("В ответе встретился посторонний ключ")
                tasks = []
                for case in batch:
                    key = case["key"]
                    tasks.append(score_case(indexed.get(key), case, labels[key], allowed[key]))
                totals = {field: sum(task[field] for task in tasks) for field in COUNT_FIELDS}
                totals["median_core"] = median(task["core"] / task["core_total"] for task in tasks)
                measurements.append({
                    "corpus": corpus, "size": size, "condition": condition, "repeat": repeat,
                    "elapsed_s": result["elapsed_s"], "usage": result["usage"],
                    "totals": totals, "tasks": tasks,
                })
    return measurements


def score_union(measurements: list[dict], labels: dict) -> list[dict]:
    joined = []
    for corpus in CORPORA:
        for size in COHORTS:
            first, second = [
                row for row in measurements
                if (row["corpus"], row["size"], row["condition"]) == (corpus, size, "baseline")
            ]
            tasks = []
            arms = ("first", "second", "merged", "added")
            for one, two in zip(first["tasks"], second["tasks"], strict=True):
                key = one["key"]
                if key != two["key"] or one["missing"] or two["missing"]:
                    raise ValueError("Для диагностики объединения нужны две полные серии")
                a, b = set(one["filed_paths"]), set(two["filed_paths"])
                gold = labels[key]
                tasks.append({
                    "key": key, "core_total": len(gold["core"]), "dep_total": len(gold["dependencies"]),
                    "metric": {
                        arm: {
                            "core": len(paths & gold["core"]),
                            "dep": len(paths & gold["dependencies"]),
                            "labelled": len(paths & (gold["core"] | gold["dependencies"])),
                            "paths": len(paths),
                        }
                        for arm, paths in zip(arms, (a, b, a | b, b - a), strict=True)
                    },
                })
            joined.append({
                "corpus": corpus, "size": size,
                "core_total": sum(task["core_total"] for task in tasks),
                "dep_total": sum(task["dep_total"] for task in tasks),
                "totals": {
                    arm: {
                        field: sum(task["metric"][arm][field] for task in tasks)
                        for field in ("core", "dep", "labelled", "paths")
                    }
                    for arm in arms
                },
                "tasks": tasks,
                "tokens": {"first": first["usage"], "second": second["usage"]},
            })
    return joined


def main() -> None:
    with gzip.open(HERE / "responses.json.gz", "rt", encoding="utf-8") as stream:
        archive = json.load(stream)
    batches, labels = make_batches()
    allowed = {case["key"]: checked_sections(case) for batch in batches.values() for case in batch}
    with TemporaryDirectory(prefix="brief-drafting-") as state:
        os.environ["XDG_STATE_HOME"] = state
        results = {
            name: score_experiment(files, name, batches, labels, allowed) for name, files in archive.items()
        }
    results["union"] = {
        "requirements-fresh-control": score_union(results["requirements"], labels),
        "anchor-copy-fresh-control": score_union(results["anchor_copy"], labels),
    }
    if results != read_json(HERE / "metrics.json"):
        raise ValueError("Пересчёт не совпал с сохранёнными метриками")
    responses = sum(len(files["responses"]) for files in archive.values())
    cases = sum(len(row["tasks"]) for name in archive for row in results[name])
    print(f"Пересчитаны {responses} пакетных ответа и {cases} брифов; метрики совпали.")


if __name__ == "__main__":
    main()
