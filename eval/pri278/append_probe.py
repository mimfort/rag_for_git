"""Pilot: add verified new dependencies while preserving the initial Luna brief."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from eval.pri278 import briefs, independent_summary, report

CONTEXT_BLOCK = re.compile(r"\n\n(?=// )")
PROMPT = """Для каждой задачи есть постановка, готовый бриф и несколько дополнительных
фрагментов. Бриф уже сохранён. Выбери только дополнительные файлы, без которых
сложнее понять или реализовать требования задачи. Файл должен давать конкретную
связь с требованиями, а не находиться просто в той же подсистеме. В JSON-поле brief
выведи только выбранные дополнительные ссылки по одной строке:
- path:line — конкретная роль в задаче
Ссылайся только на переданные дополнительные фрагменты и видимые строки.
Оставь поле brief пустым, если дополнительных файлов для этой задачи нет.
Не переписывай и не дублируй готовый бриф. Задачи в пакете независимы.
"""


def build_batch(cases, control, expanded, previous):
    result = []
    for case in cases:
        key = case["key"]
        base = control[key]["modes"]["hybrid"]
        new = expanded[key]["modes"]["hybrid"]
        extra = set(new["context_paths"]) - set(base["context_paths"])
        if not extra:
            continue
        chunks = []
        for block in CONTEXT_BLOCK.split(new["context"]):
            anchor = briefs.ANCHOR.match(block)
            if anchor and anchor[1] in extra:
                chunks.append(block)
        result.append({
            "key": key, "task": case["input"], "existing_brief": previous[key]["brief"],
            "context": "\n\n".join(chunks),
        })
    return result


def merge_citations(existing, proposed):
    # References were checked against separate inputs. Keep the strongest evidence.
    strength = {"path_unseen": 0, "outside_excerpt": 1, "verified": 2}
    by_reference = {(item["path"], item["line"]): item for item in existing}
    for item in proposed:
        key = item["path"], item["line"]
        current = by_reference.get(key)
        if current is None or strength[item["status"]] > strength[current["status"]]:
            by_reference[key] = item
    return list(by_reference.values())


def summarize(batch, model_results, cases, control, expanded, baseline):
    row_by_key = {row["key"]: row for row in batch}
    by_key = {row["key"]: row for row in model_results}
    new_results = []
    before, after = [], []
    for case in cases:
        key = case["key"]
        a, b = control[key]["modes"]["hybrid"], expanded[key]["modes"]["hybrid"]
        existing = briefs.check_references(baseline[key]["brief"], a["context"])
        historical = report.historical_brief_paths(baseline[key], a)
        proposed = []
        if key in row_by_key:
            proposed = briefs.check_references(by_key[key]["brief"], row_by_key[key]["context"])
            accepted = briefs.verified_paths(proposed) - briefs.verified_paths(existing)
            core, deps = report.labels(case)
            new_results.append({
                "key": key, "proposals": [
                    {"section": "code", "path": item["path"], "line": item["line"],
                     "status": item["status"]}
                    for item in proposed
                ],
                "accepted": sorted(accepted), "core": sorted(accepted & core),
                "dependencies": sorted(accepted & deps),
                "unlabeled": sorted(accepted - (core | deps)),
            })
        before.append(report.add_citation_row(case, a, existing, historical_paths=historical))
        # The archived pilot checked new citations, but credited its initial brief by path.
        after.append(report.add_citation_row(
            case, b, merge_citations(existing, proposed),
            historical_paths=historical | briefs.verified_paths(proposed),
        ))
    return {
        "brief_coverage_version": briefs.COVERAGE_VERSION,
        "comparisons": independent_summary.compare(before, after),
        "additions": new_results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    corpus = Path(__file__).with_name("independent-corpus.json")
    cases = report.read(corpus)
    control, expanded = (report.read_run(work, label) for label in ("all-tail-0", "all-tail-3"))
    baseline = report.model_index(report.model_batches(work, "all-30"))
    baseline.update(report.model_index(report.model_batches(work, "all-tail-0")))
    batch = build_batch(cases, control, expanded, baseline)
    invocation = work / "luna-append-invocation"
    invocation.mkdir(exist_ok=True)
    briefs._write_json(invocation / "schema.json", briefs.SCHEMA)
    target = work / "append-batch.json"
    briefs.run_batch(batch, invocation, target, prompt=PROMPT)
    measured = report.read(target)
    output = summarize(batch, measured["briefs"], cases, control, expanded, baseline)
    output["usage"] = measured["usage"]
    output["model"] = measured["model"]
    (work / "append-results.json").write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
