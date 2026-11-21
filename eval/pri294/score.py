"""Покрытие критериев PRI-294 запросами, строками кода и ссылками брифа."""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path
from statistics import median

from eval.pri294.replay import (
    AFFECTED, BASE_SHA, CASES, CONTROLS, EVIDENCE, old_builder, query_sets, read, write,
)

ACCEPTANCE = re.compile(r"^#{1,6}[ \t]+.*(?:критери|при[её]мк|acceptance).*?$", re.I | re.M)
HEADING = re.compile(r"^#{1,6}[ \t]+", re.M)
ITEM = re.compile(r"^[ \t]*(?:\d+[.)]|[-*+])[ \t]+(\S.*?)\s*$")
HEADER = re.compile(r"^// .+ \((?P<path>.+):(?P<first>\d+)-(?P<last>\d+)\)$")
ROW = re.compile(r"^\s*(\d+) \| (.*)$")
REFERENCE = re.compile(r"^\s*-\s+`?([^\s`]+):(\d+)`?\s+(?:—|–|-)\s+", re.M)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(text):
    return " ".join(text.casefold().split())


def frozen_criteria(task):
    """Фиксированная форма корпуса — один элемент списка на одной строке."""
    text = task["description"]
    if not (heading := ACCEPTANCE.search(text)) or task["criteria"]:
        raise ValueError("Корпус должен содержать один инлайн-список критериев")
    section = HEADING.split(text[heading.end():], maxsplit=1)[0]
    criteria = []
    for line in section.splitlines():
        if not line.strip():
            continue
        if not (item := ITEM.fullmatch(line)):
            raise ValueError("В корпусе появился критерий вне одноуровневого списка")
        criteria.append(item[1])
    return criteria


def context_blocks(context):
    """Выделить целиком видимые строки отдельно для каждого фрагмента."""
    lines = context.splitlines()
    if "[...truncated]" in lines:
        lines = lines[:max(0, lines.index("[...truncated]") - 1)]
    blocks = []
    block = None
    for line in lines:
        if header := HEADER.fullmatch(line):
            first, last = int(header["first"]), int(header["last"])
            block = {}
            blocks.append((header["path"], block))
        elif block is not None and (row := ROW.fullmatch(line)):
            number = int(row[1])
            if first <= number <= last:
                block[number] = row[2]
        else:
            block = None
    return blocks


def locations(blocks, anchors):
    """Вернуть точные строки якорей и все видимые строки их фрагментов."""
    exact = set()
    ranges = set()
    for path, block in blocks:
        for anchor in anchors:
            if path != anchor["path"]:
                continue
            hits = {(path, line) for line, text in block.items() if anchor["contains"] in text}
            if hits:
                exact.update(hits)
                ranges.update((path, line) for line in block)
    return sorted(exact), ranges


def score_case(row, brief, criteria, labels, eligible):
    query_norms = [normalize(query) for query in row["queries"]]
    excluded = {normalize(text) for text in row["excluded"]}
    represented = [index for index, text in enumerate(criteria, 1)
                   if any(normalize(text) in query for query in query_norms)]
    documented = [index for index, text in enumerate(criteria, 1)
                  if normalize(text) in excluded]
    blocks = context_blocks(row["context"])
    visible = {(path, line) for path, block in blocks for line in block}
    references = {(path, int(line)) for path, line in REFERENCE.findall(brief)}
    grounded = references & visible
    evidence = []
    for label in labels:
        anchors = label["anchors"]
        exact, fragment = locations(blocks, anchors)
        evidence.append({
            "criterion": label["criterion"],
            "eligible": any(anchor["path"] in eligible for anchor in anchors),
            "code": exact,
            "brief_fragment": sorted(grounded & fragment),
            "brief_exact": sorted(grounded & set(exact)),
        })
    return {
        "query_sha256": [sha(query) for query in row["queries"]],
        "query_count": len(row["queries"]),
        "group_count": sum("\n" in query for query in row["queries"][1:]),
        "criteria_total": len(criteria),
        "represented": represented,
        "documented": documented,
        "context_sha256": sha(row["context"]),
        "context_files": len({path for path, block in blocks if block}),
        "context_chars": len(row["context"]),
        "grounded_citations": len(grounded),
        "invalid_citations": sorted(references - visible),
        "evidence": evidence,
        "retrieval_seconds": row["seconds"],
    }


def summarize(rows, scope):
    cases = [rows[key] for key in scope]
    all_evidence = [label for row in cases for label in row["evidence"]]
    eligible = [label for label in all_evidence if label["eligible"]]
    return {
        "criteria_total": sum(row["criteria_total"] for row in cases),
        "represented": sum(len(row["represented"]) for row in cases),
        "documented": sum(len(row["documented"]) for row in cases),
        "query_count": sum(row["query_count"] for row in cases),
        "group_count": sum(row["group_count"] for row in cases),
        "context_files_median": median(row["context_files"] for row in cases),
        "context_chars_median": median(row["context_chars"] for row in cases),
        "code_evidence": sum(bool(row["code"]) for row in all_evidence),
        "brief_evidence": sum(bool(row["brief_fragment"]) for row in all_evidence),
        "brief_exact": sum(bool(row["brief_exact"]) for row in all_evidence),
        "evidence_total": len(all_evidence),
        "eligible_evidence": len(eligible),
        "grounded_citations": sum(row["grounded_citations"] for row in cases),
        "invalid_citations": sum(len(row["invalid_citations"]) for row in cases),
        "retrieval_seconds_median": round(median(median(row["retrieval_seconds"])
                                                 for row in cases), 6),
        "parser_microseconds_median": round(median(median(row["parser_microseconds"])
                                                   for row in cases), 2),
    }


def model_results(work, arm, arm_rows):
    batches = [read(path) for path in sorted(work.glob(f"brief-{arm}-*.json"))]
    for batch in batches:
        hashes = {key: sha(arm_rows[key]["context"]) for key in batch["briefs"]}
        if batch.get("context_sha256") != hashes:
            raise ValueError(f"{arm}: брифы сгенерированы по другой выдаче")
    briefs = {key: text for batch in batches for key, text in batch["briefs"].items()}
    if set(briefs) != set(CASES) or sum(len(batch["briefs"]) for batch in batches) != len(CASES):
        raise ValueError(f"{arm}: отсутствует полный набор брифов")
    return briefs, [{name: batch[name] for name in ("model", "effort", "elapsed_seconds", "usage")}
                    for batch in batches]


def build_results(work):
    snapshot = read(work / "snapshot.json")
    retrieval = read(work / "retrieval.json")
    keys = set(CASES)
    if keys != set(snapshot["tasks"]):
        raise ValueError("Набор карточек изменился")
    before = old_builder()
    candidate_equivalent = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", "reviewer/mcp/subqueries.py"], text=True,
    ).strip()
    for key, row in retrieval["arms"]["candidate"].items():
        if query_sets(snapshot["tasks"][key], key, "candidate", before) != (
            row["queries"], row["excluded"],
        ):
            raise ValueError(f"{key}: сохранённый вариант отличается от рабочего")
    eligible = set(retrieval["eligible_paths"])
    evidence = read(EVIDENCE)
    result = {
        "baseline_sha": BASE_SHA,
        "candidate_measured_sha": retrieval["candidate_sha"],
        "candidate_verified_queries_sha": candidate_equivalent,
        "index": snapshot["index"],
        "controls_identical_code": {
            key: retrieval["arms"]["baseline"][key]["context"]
            == retrieval["arms"]["candidate"][key]["context"] for key in CONTROLS
        },
        "snapshot_sha256": sha((work / "snapshot.json").read_text(encoding="utf-8")),
        "limits": snapshot["limits"],
        "embedding": snapshot["embedding"],
        "embedding_prewarm_seconds": retrieval["embeddings_seconds"],
        "unique_query_count": retrieval["unique_queries"],
        "eligible_paths": sorted(eligible),
        "arms": {},
    }
    for arm, arm_rows in retrieval["arms"].items():
        briefs, batches = model_results(work, arm, arm_rows)
        rows = {}
        for key in CASES:
            labels = evidence[key]
            criteria = frozen_criteria(snapshot["tasks"][key])
            if any(not 1 <= label["criterion"] <= len(criteria) for label in labels):
                raise ValueError(f"{key}: размечен отсутствующий критерий")
            rows[key] = score_case(arm_rows[key], briefs[key], criteria, labels, eligible)
            rows[key]["parser_microseconds"] = retrieval["parser_microseconds"][arm][key]
            accounted = set(rows[key]["represented"]) | set(rows[key]["documented"])
            if arm == "candidate" and len(accounted) != len(criteria):
                raise ValueError(f"{key}: не все критерии видны или объяснены")
        result["arms"][arm] = {
            "model_batches": batches,
            "affected": summarize(rows, AFFECTED),
            "control": summarize(rows, CONTROLS),
            "cases": rows,
        }

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write(args.output, build_results(args.work))


if __name__ == "__main__":
    main()
