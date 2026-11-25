"""Сопоставить выбор отрывка и проверенные ссылки на фиксированных задачах."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from unittest.mock import patch

from eval.pri278.briefs import (
    COVERAGE_VERSION,
    EFFORT,
    MODEL,
    PROMPT,
    SCHEMA,
    parsed_references,
    run_batch,
)
from eval.pri278.replay import blob_reader, make_item, snapshot_sources
from eval.pri324.selector import choose_windows
from reviewer.index.chunker import chunk_python
from reviewer.mcp.brief_anchors import check_brief_anchors
from reviewer.mcp.brief_evidence import BriefEvidence, _visible_lines
from reviewer.mcp.schemas import BriefReferenceIn

_ARCHIVES = {
    "main": ("corpus.json", "pool_size/evidence.jsonl.gz", "run", "all-pool-30"),
    "independent": (
        "independent-corpus.json",
        "independent/evidence.jsonl.gz",
        "variant",
        "all-30",
    ),
}
_BATCHES = {
    "main": (
        ("PRI-196", "PRI-215", "PRI-223", "PRI-225"),
        ("PRI-257", "PRI-228", "PRI-241", "PRI-275"),
    ),
    "independent": (("PRI-170", "PRI-202", "PRI-221", "PRI-179", "PRI-205"),),
}
_ARMS = ("base", "reframe")
_REPEATS = (1, 2)


def _read_archives(root: Path) -> dict:
    selected = {}
    for cohort, (labels, archive, run_field, run_name) in _ARCHIVES.items():
        cases = {case["key"]: case for case in json.loads((root / labels).read_text())}
        with gzip.open(root / archive, "rt", encoding="utf-8") as file:
            rows = (json.loads(line) for line in file)
            modes = {
                row["retrieval"]["key"]: row["retrieval"]
                for row in rows
                if row[run_field] == run_name and "retrieval" in row
            }
        for batch in _BATCHES[cohort]:
            for key in batch:
                case, retrieval = cases[key], modes[key]
                if case["base_sha"] != retrieval["base_sha"]:
                    raise ValueError(f"Другой Git-снимок для {key}")
                selected[key] = {
                    "case": case,
                    "base": retrieval["modes"]["hybrid"]["context"],
                }
    return selected


def _prepare(archives: Path) -> dict:
    cases = _read_archives(archives)
    with blob_reader() as read:
        blobs, chunks = {}, {}
        for data in cases.values():
            case, base = data["case"], data["base"]
            sources = snapshot_sources(case["base_sha"], read, blobs)
            paths = list(_visible_lines(base))
            for path in paths:
                key = (path, sources[path])
                if key not in chunks:
                    chunks[key] = [
                        make_item(chunk) for chunk in chunk_python(path, sources[path].encode())
                    ]
            alternatives = {path: chunks[path, sources[path]] for path in paths}
            data["reframe"] = choose_windows(base, case["input"], sources, alternatives)
            for arm in _ARMS:
                section = data[arm]
                eligible, diagnostic = check_brief_anchors(
                    {"code": section},
                    lambda paths, source=sources: {path: source[path] for path in paths},
                )
                if not eligible.get("code") or eligible["code"] != _visible_lines(section):
                    raise ValueError(f"{case['key']}, {arm}: фрагмент расходится с Git")
                data[f"{arm}_git_lines"] = diagnostic["code"]["checked"]
                data[f"{arm}_eligible"] = eligible
            if list(_visible_lines(data["reframe"])) != paths or len(data["reframe"]) > len(base):
                raise ValueError(f"{case['key']}: изменились пути или превышен бюджет")
    return cases


def _schedule(repeat: int, index: int) -> tuple[str, str]:
    return _ARMS if (repeat + index) % 2 else _ARMS[::-1]


def _jobs():
    for cohort, batches in _BATCHES.items():
        for index, keys in enumerate(batches, 1):
            for repeat in _REPEATS:
                yield cohort, index, keys, repeat


def _batch(cases, keys, arm):
    return [
        {"key": key, "task": cases[key]["case"]["input"], "context": cases[key][arm]}
        for key in keys
    ]


def _stem(cohort, repeat, index, arm) -> str:
    return f"{cohort}-r{repeat}-b{index}-{arm}"


def _digest_text(text):
    return sha256(text.encode()).hexdigest()


def _run(cases: dict, work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    protocol = {
        "model": MODEL,
        "effort": EFFORT,
        "client": subprocess.check_output(["codex", "--version"], text=True).strip(),
        "prompt_sha256": _digest_text(PROMPT),
        "schema_sha256": _digest_text(json.dumps(SCHEMA)),
        "batch_input_sha256": {
            _stem(cohort, repeat, index, arm): _digest_text(
                json.dumps(_batch(cases, keys, arm), ensure_ascii=False),
            )
            for cohort, index, keys, repeat in _jobs()
            for arm in _ARMS
        },
    }
    lock = work / "protocol.json"
    if lock.exists() and json.loads(lock.read_text()) != protocol:
        raise ValueError("Рабочий каталог содержит другой протокол")
    _write(lock, protocol)

    def run_pair(cohort, index, keys, repeat):
        for arm in _schedule(repeat, index):
            output = work / f"{_stem(cohort, repeat, index, arm)}.json"
            run_batch(_batch(cases, keys, arm), invocation, output)

    with TemporaryDirectory(prefix="brief-windows-codex-") as temp:
        invocation = Path(temp)
        _write(invocation / "schema.json", SCHEMA, indent=None, trailing=False)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run_pair, *job) for job in _jobs()]
            for job in futures:
                job.result()
    batches = {
        stem: json.loads((work / f"{stem}.json").read_text())
        for stem in protocol["batch_input_sha256"]
    }
    responses = {"protocol": protocol, "batches": batches}
    responses["response_sha256"] = _response_digests(responses)
    return responses


def _response_digests(responses):
    items = dict(responses["batches"])
    for label, pair in responses.get("controls", {}).items():
        items.update({f"controls:{label}:{arm}": pair[arm] for arm in _ARMS})
    return {
        stem: _digest_text(json.dumps(response, sort_keys=True, ensure_ascii=False))
        for stem, response in items.items()
    }


def _validate_archive(responses):
    if responses.get("response_sha256") != _response_digests(responses):
        raise ValueError("Нарушены контрольные суммы ответов одного или нескольких запусков")


def _metrics(case, answer, eligible, evidence: BriefEvidence):
    evidence_id = evidence.save_sections(eligible)
    if evidence_id is None:
        raise ValueError(f"{case['key']}: координаты не сохранены")
    references = [
        BriefReferenceIn(section="code", path=path, line=line, reason=reason)
        for path, line, reason in parsed_references(answer["brief"])
    ]
    verified = evidence.verify_references(evidence_id, references)
    if verified.get("status") != "ok" or verified.get("render_status") not in ("ok", "empty"):
        raise ValueError(f"{case['key']}: ошибка проверки ссылок")
    paths = {
        citation["path"] for citation in verified["citations"] if citation["status"] == "verified"
    }
    if paths != set(answer["verified_paths"]):
        raise ValueError(f"{case['key']}: расходятся результаты проверки ссылок")
    core = {row["path"] for row in case["obligatory"]}
    deps = {row["path"] for row in case["read_only"]}
    return {
        "core": {"hit": len(paths & core), "all": len(core)},
        "dependencies": {"hit": len(paths & deps), "all": len(deps)},
        "labelled_paths": len(paths & (core | deps)),
        "verified_paths": len(paths),
        "rejected_references": len(references)
        - sum(citation["status"] == "verified" for citation in verified["citations"]),
    }


def _summarize(rows):
    metrics = {}
    for name in ("core", "dependencies"):
        metrics[name] = {field: sum(row[name][field] for row in rows) for field in ("hit", "all")}
    by_key = {
        key: [row for row in rows if row["key"] == key] for key in {row["key"] for row in rows}
    }
    rates = [
        sum(row["core"]["hit"] for row in pair) / sum(row["core"]["all"] for row in pair)
        for pair in by_key.values()
    ]
    metrics["task_median_core"] = median(rates)
    for name in ("labelled_paths", "verified_paths", "rejected_references", "chars", "git_lines"):
        metrics[name] = sum(row[name] for row in rows)
    return {"tasks": len(by_key), "briefs": len(rows), **metrics}


def _validate_envelope(response, protocol, keys):
    if (response.get("model"), response.get("reasoning_effort")) != (
        protocol["model"],
        protocol["effort"],
    ):
        raise ValueError("Модель или режим ответа расходится с протоколом")
    first, final = (response["coverage"][name] for name in ("first", "final"))
    known = frozenset(keys)
    for coverage in (first, final):
        if coverage["expected"] != len(known) or coverage["statuses"].keys() != known:
            raise ValueError("Покрытие ответа расходится со списком входных ключей")
    valid = {"verified", "no_verified_refs"}
    missing = {key for key, status in first["statuses"].items() if status == "missing"}
    attempts = response["retry_attempts"]
    if attempts.keys() != missing or first["returned"] != sum(
        status in valid for status in first["statuses"].values()
    ):
        raise ValueError("Данные о пропусках ответа и повторных запросах расходятся")
    actual = first["statuses"].copy()
    usage = Counter(response["first_usage"])
    unknown = int(not response["first_usage"])
    for key, attempt in attempts.items():
        if attempt["state"] != "completed" or attempt["statuses"].keys() != {key}:
            raise ValueError(f"Некорректный повторный запрос для {key}")
        actual.update(attempt["statuses"])
        usage.update(attempt["usage"])
        unknown += not attempt["usage"]
    if (
        actual != final["statuses"]
        or final["returned"] != len(known)
        or not set(actual.values()) <= valid
    ):
        raise ValueError("Итоговый пакет брифов неполон")
    if dict(usage) != response["usage"] or unknown != response["unknown_usage_attempts"]:
        raise ValueError("Учёт токенов повторных запросов расходится с исходным ответом")


def _score_batch(cases, keys, arm, response, protocol, evidence):
    _validate_envelope(response, protocol, keys)
    fingerprint = _digest_text(
        json.dumps(
            [
                protocol["model"],
                protocol["effort"],
                COVERAGE_VERSION,
                PROMPT,
                protocol["schema_sha256"],
                _batch(cases, keys, arm),
            ],
            ensure_ascii=False,
        )
    )
    if response["input_sha256"] != fingerprint:
        raise ValueError(f"Ответ соответствует другому запуску ({arm}, {keys})")
    briefs = {answer["key"]: answer for answer in response["briefs"]}
    if len(response["briefs"]) != len(keys) or set(briefs) != set(keys):
        raise ValueError(f"Неполный парный ответ ({arm}, {keys})")
    rows = []
    for key in keys:
        data = cases[key]
        case = data["case"]
        section = data[arm]
        rows.append(
            {
                "key": key,
                "arm": arm,
                "large": len(case["obligatory"]) >= 10,
                "retried": key in response["retry_attempts"],
                "base_sha": case["base_sha"],
                "context_sha256": _digest_text(section),
                "chars": len(section),
                "git_lines": data[f"{arm}_git_lines"],
                **_metrics(case, briefs[key], data[f"{arm}_eligible"], evidence),
            }
        )
    return rows


def _usage(response):
    return {
        "elapsed_s": response["batch_elapsed_s"],
        "first_returned": response["coverage"]["first"]["returned"],
        "final_returned": response["coverage"]["final"]["returned"],
        "retried_keys": sorted(response["retry_attempts"]),
        **response["usage"],
    }


def _score_controls(cases, controls, protocol, evidence):
    results = {}
    for label, pair in controls.items():
        results[label] = {
            arm: {
                "usage": _usage(pair[arm]),
                "per_brief": _score_batch(cases, pair["keys"], arm, pair[arm], protocol, evidence),
            }
            for arm in _ARMS
        }
    return results


def _score(cases, responses, evidence: BriefEvidence):
    _validate_archive(responses)
    protocol = responses["protocol"]
    if protocol["prompt_sha256"] != _digest_text(PROMPT) or protocol[
        "schema_sha256"
    ] != _digest_text(json.dumps(SCHEMA)):
        raise ValueError("Промпт или схема расходятся с архивом ответов")
    rows, usage = [], []
    for cohort, index, keys, repeat in _jobs():
        for arm in _ARMS:
            stem = _stem(cohort, repeat, index, arm)
            request = _batch(cases, keys, arm)
            if protocol["batch_input_sha256"][stem] != _digest_text(
                json.dumps(request, ensure_ascii=False)
            ):
                raise ValueError(f"Подготовленный вход расходится с {stem}")
            response = responses["batches"][stem]
            usage.append({"cohort": cohort, "repeat": repeat, "arm": arm, **_usage(response)})
            rows.extend(
                {"cohort": cohort, "repeat": repeat, **row}
                for row in _score_batch(
                    cases,
                    keys,
                    arm,
                    response,
                    protocol,
                    evidence,
                )
            )

    groups = {
        cohort: {
            arm: {
                name: _summarize(
                    [
                        row
                        for row in rows
                        if row["arm"] == arm
                        and (cohort == "all" or row["cohort"] == cohort)
                        and (group is None or row["large"] is group)
                    ]
                )
                for name, group in (("all", None), ("large", True), ("ordinary", False))
            }
            for arm in _ARMS
        }
        for cohort in ("all", *_BATCHES)
    }
    return {
        "groups": groups,
        "per_brief": rows,
        "usage": usage,
        "controls": _score_controls(cases, responses.get("controls", {}), protocol, evidence),
    }


def _write(path, data, *, indent=2, trailing=True):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent) + ("\n" if trailing else ""),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "score"))
    parser.add_argument("--archives", type=Path, default=Path("eval/pri278"))
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    cases = _prepare(args.archives)
    artifact = args.work / "responses.json.gz"
    if args.command == "run":
        responses = _run(cases, args.work)
        with gzip.open(artifact, "wt", encoding="utf-8") as file:
            json.dump(responses, file, ensure_ascii=False)
    else:
        with gzip.open(artifact, "rt", encoding="utf-8") as file:
            responses = json.load(file)
    with TemporaryDirectory(prefix="brief-windows-evidence-") as temp:
        with patch.dict(os.environ, {"XDG_STATE_HOME": temp}):
            evidence = BriefEvidence()
        _write(args.work / "metrics.json", _score(cases, responses, evidence))


if __name__ == "__main__":
    main()
