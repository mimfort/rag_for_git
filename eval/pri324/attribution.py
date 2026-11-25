"""Разобрать изменения ссылок PRI-324 и перепроверить одиночные ответы PRI-221."""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from unittest.mock import patch

from eval.pri278.briefs import EFFORT, MODEL, PROMPT, SCHEMA, parsed_references
from eval.pri324.replay import (
    _ARMS,
    _batch,
    _digest_text,
    _jobs,
    _prepare,
    _response_digests,
    _score,
    _score_batch,
    _stem,
)
from reviewer.mcp.brief_evidence import _HEADER, BriefEvidence

_CASE = "PRI-221"
_ANCHOR = ("reviewer/mcp/session_serde.py", 42)
_REPEATS = (1, 2, 3, 4)
_SINGLETON_ARCHIVE = Path(__file__).parents[1] / "pri325" / "singleton-responses.json.gz"


def _blocks(context: str) -> dict:
    blocks = {}
    for block in context.split("\n\n"):
        header = _HEADER.fullmatch(block.partition("\n")[0])
        if header is None or header["path"] in blocks:
            raise ValueError("Ожидается один полный фрагмент на путь")
        blocks[header["path"]] = block
    return blocks


def _references(answer, eligible):
    references, verified = defaultdict(list), defaultdict(list)
    for path, line, reason in parsed_references(answer["brief"]):
        row = {"line": line, "reason": reason}
        references[path].append(row)
        if line in eligible.get(path, ()):
            verified[path].append(row)
    if verified.keys() != set(answer["verified_paths"]):
        raise ValueError("Архивный набор подтверждённых путей расходится с видимыми якорями Git")
    return references, verified


def attribute_pair(case: dict, original: dict, candidate: dict) -> dict:
    """Локализовать различия по наблюдаемым строкам без причинного вывода."""
    core = {row["path"] for row in case["case"]["obligatory"]}
    blocks = {arm: _blocks(case[arm]) for arm in _ARMS}
    eligible = {arm: case[f"{arm}_eligible"]["code"] for arm in _ARMS}
    references, verified = {}, {}
    for arm, answer in zip(_ARMS, (original, candidate), strict=True):
        references[arm], verified[arm] = _references(answer, eligible[arm])

    events = []
    for own, other, direction in (
        ("base", "reframe", "loss"), ("reframe", "base", "gain"),
    ):
        for path in sorted((verified[own].keys() - verified[other].keys()) & core):
            anchors = verified[own][path]
            other_lines = eligible[other].get(path, set())
            matching = sorted({row["line"] for row in anchors} & other_lines)
            other_references = references[other].get(path, [])
            if not other_lines:
                kind = "path_unavailable"
            elif not matching:
                kind = "anchor_unavailable"
            elif other_references:
                kind = "rejected"
            else:
                kind = "omitted"
            events.append({
                "direction": direction,
                "path": path,
                "kind": kind,
                "window_unchanged": blocks[own][path] == blocks[other].get(path),
                "same_anchor_lines": matching,
                "verified_references": anchors,
                "other_references": other_references,
                "visible_ranges": {
                    arm: [min(lines), max(lines)] if (lines := eligible[arm].get(path)) else None
                    for arm in _ARMS
                },
            })
    return {
        "key": case["case"]["key"],
        "identical_task_input": case["base"] == case["reframe"],
        "changed_windows": sorted(
            path for path in blocks["base"].keys() | blocks["reframe"].keys()
            if blocks["base"].get(path) != blocks["reframe"].get(path)
        ),
        "core": {
            **{arm: len(core & verified[arm].keys()) for arm in _ARMS},
            "all": len(core),
        },
        "events": events,
    }


def _aggregate_tasks(rows, *, reuse_identical=False):
    counts = defaultdict(Counter)
    for row in rows:
        values = row["core"]
        if reuse_identical and row["identical_task_input"]:
            values = {**values, "reframe": values["base"]}
        counts[row["key"]].update(values)

    rates = {
        key: {arm: Fraction(counts[key][arm], counts[key]["all"]) for arm in _ARMS}
        for key in sorted(counts)
    }
    medians = {arm: median(parts[arm] for parts in rates.values()) for arm in _ARMS}
    deltas = {key: parts["reframe"] - parts["base"] for key, parts in rates.items()}
    return {
        "micro_core": {field: sum(v[field] for v in counts.values()) for field in (*_ARMS, "all")},
        "task_medians": {arm: float(value) for arm, value in medians.items()},
        "difference_of_arm_medians": float(medians["reframe"] - medians["base"]),
        "median_paired_task_delta": float(median(deltas.values())),
        "task_rates": {
            key: {
                **{arm: float(parts[arm]) for arm in _ARMS},
                "paired_delta": float(deltas[key]),
            }
            for key, parts in rates.items()
        },
    }


def summarize_pairs(rows: list[dict]) -> dict:
    """Сравнить медианы уровней с медианой изменений по парным задачам."""
    aggregate = _aggregate_tasks(rows)
    sensitivity = _aggregate_tasks(rows, reuse_identical=True)
    return {
        **aggregate,
        "identical_input_sensitivity": {
            "identical_observations": sum(row["identical_task_input"] for row in rows),
            "micro_core": sensitivity["micro_core"],
            "task_medians": sensitivity["task_medians"],
        },
        "events_by_direction_and_kind": dict(sorted(Counter(
            f"{event['direction']}:{event['kind']}" for row in rows for event in row["events"]
        ).items())),
    }


def _retrospective(cases, archive, evidence):
    _score(cases, archive, evidence)
    rows = []
    fingerprints = archive["protocol"]["batch_input_sha256"]
    for cohort, batch, keys, repeat in _jobs():
        stems = {arm: _stem(cohort, repeat, batch, arm) for arm in _ARMS}
        briefs = {
            arm: {row["key"]: row for row in archive["batches"][stem]["briefs"]}
            for arm, stem in stems.items()
        }
        same_batch = fingerprints[stems["base"]] == fingerprints[stems["reframe"]]
        for key in keys:
            rows.append({
                "cohort": cohort,
                "batch": batch,
                "repeat": repeat,
                "identical_batch_input": same_batch,
                **attribute_pair(cases[key], briefs["base"][key], briefs["reframe"][key]),
            })

    cohorts = {
        name: summarize_pairs([
            row for row in rows
            if (name == "all" or row["cohort"] == name) and row["core"]["all"] >= 10
        ])
        for name in ("all", "main", "independent")
    }
    return {"large": cohorts, "pairs": rows, "batch_input_sha256": fingerprints}


def _singletons(cases, archive, evidence):
    protocol = archive["protocol"]
    requests = {
        arm: _digest_text(json.dumps(_batch(cases, (_CASE,), arm), ensure_ascii=False))
        for arm in _ARMS
    }
    expected_protocol = {
        "case": _CASE,
        "base_sha": cases[_CASE]["case"]["base_sha"],
        "model": MODEL,
        "effort": EFFORT,
        "reps": len(_REPEATS),
        "schedule": ["AB", "BA", "AB", "BA"],
        "endpoint": "singleton",
        "path": _ANCHOR[0],
        "prompt_sha256": _digest_text(PROMPT),
        "schema_sha256": _digest_text(json.dumps(SCHEMA)),
        "input_sha256": requests,
    }
    if protocol != expected_protocol:
        raise ValueError("Протокол одиночных запросов расходится с исходными Git-слепками")

    responses = archive["responses"]
    expected_keys = {f"r{repeat}-{arm}" for repeat in _REPEATS for arm in _ARMS}
    if (
        responses.keys() != expected_keys
        or archive["response_sha256"] != _response_digests({"batches": responses})
    ):
        raise ValueError("Архив одиночных ответов неполон или контрольные суммы расходятся")

    rows = []
    for repeat in _REPEATS:
        for arm in _ARMS:
            response = responses[f"r{repeat}-{arm}"]
            if response["retry_attempts"]:
                raise ValueError("Одиночный ответ содержит повторный запрос")
            metrics, = _score_batch(cases, (_CASE,), arm, response, protocol, evidence)
            answer, = response["briefs"]
            anchor_verified = any(
                (path, line) == _ANCHOR for path, line, _ in parsed_references(answer["brief"])
            ) and _ANCHOR[1] in cases[_CASE][f"{arm}_eligible"]["code"].get(_ANCHOR[0], ())
            rows.append({"repeat": repeat, "focus_anchor_verified": anchor_verified, **metrics})
    return rows


def _read_archive(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=Path, default=Path("eval/pri278"))
    parser.add_argument("--responses", type=Path, default=Path(__file__).with_name("responses.json.gz"))
    parser.add_argument("--singletons", type=Path, default=_SINGLETON_ARCHIVE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    cases = _prepare(args.archives)
    with TemporaryDirectory(prefix="brief-attribution-evidence-") as temp:
        with patch.dict("os.environ", {"XDG_STATE_HOME": temp}):
            evidence = BriefEvidence()
        result = {
            "retrospective": _retrospective(cases, _read_archive(args.responses), evidence),
            "singletons": _singletons(cases, _read_archive(args.singletons), evidence),
        }
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
