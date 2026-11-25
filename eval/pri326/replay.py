"""Офлайн-оценка брифов по зафиксированным задачам, контекстам и ответам."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from unittest.mock import patch

from eval.pri278.briefs import EFFORT, MODEL, PROMPT, SCHEMA, _write_bytes, parsed_references
from reviewer.gitutil import file_at_ref, rev_parse
from reviewer.mcp.brief_anchors import check_brief_anchors
from reviewer.mcp.brief_evidence import BriefEvidence, _visible_lines
from reviewer.mcp.schemas import BriefReferenceIn
from reviewer.retrieval.retriever import _is_test_path

ROOT = Path(__file__).resolve().parents[2]
REPO = "mimfort/rag_for_git"
ARMS = ("base", "reframe")
BULK_MIN = 10
RECEIVED = frozenset({"verified", "no_verified_refs"})
NOT_RECEIVED = frozenset({"missing", "duplicate", "invalid", "blank"})
SHA = re.compile(r"[0-9a-f]{40}")
CASE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _digest(value, *, canonical=False):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=canonical).encode()).hexdigest()


def _historical_keys():
    directory = ROOT / "eval" / "pri278"
    return {
        case["key"]
        for filename in ("corpus.json", "independent-corpus.json")
        for case in json.loads((directory / filename).read_text(encoding="utf-8"))
    }


def _labelled_paths(case, field):
    paths = [item["path"] for item in case[field]]
    if len(set(paths)) != len(paths) or any(
        not isinstance(path, str) or not path or "\x00" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        for path in paths
    ):
        raise ValueError(f"{case['key']}: некорректные пути разметки {field}")
    return set(paths)


def _validate_cases(cases):
    old = _historical_keys()
    seen = set()
    if not cases:
        raise ValueError("Пустой корпус")
    for case in cases:
        key = case["key"]
        if not CASE_KEY.fullmatch(key) or key in seen:
            raise ValueError(f"Некорректный или повторный ключ задачи {key}")
        seen.add(key)
        aliases = set(case.get("task_keys", (key,))) | {key}
        if case.get("repo", REPO) == REPO and aliases & old:
            raise ValueError(f"{key}: задача уже входила в корпус PRI-278/283")
        if not SHA.fullmatch(case["base_sha"]) or not case["input"].strip():
            raise ValueError(f"{key}: нужен точный Git SHA и текст задачи")
        if "solution_sha" in case and not SHA.fullmatch(case["solution_sha"]):
            raise ValueError(f"{key}: нужен точный Git SHA исправления")
        core = _labelled_paths(case, "obligatory")
        if not core or core & _labelled_paths(case, "read_only"):
            raise ValueError(f"{key}: пустое ядро или пересечение ядра с зависимостями")
        contexts = case["contexts"]
        if contexts.keys() != set(ARMS) or not all(isinstance(text, str) for text in contexts.values()):
            raise ValueError(f"{key}: ожидаются две зафиксированные секции кода")
        base, treatment = (contexts[arm] for arm in ARMS)
        paths = list(_visible_lines(base))
        if not paths or paths != list(_visible_lines(treatment)) or len(treatment) > len(base):
            raise ValueError(f"{key}: изменился порядок путей или превышен бюджет")


def _jobs(cases, repeats):
    return {
        f"{case['key']}-r{repeat}-{arm}": (case, repeat, arm)
        for case in cases
        for repeat in repeats
        for arm in ARMS
    }


def _protocol(cases, responses, repeats):
    if not repeats or any(type(rep) is not int or rep <= 0 for rep in repeats) or len(set(repeats)) != len(repeats):
        raise ValueError("Протокол требует уникальные положительные номера повторов")
    return {
        "version": 1,
        "model": MODEL,
        "effort": EFFORT,
        "repeats": list(repeats),
        "arms": list(ARMS),
        "prompt_sha256": sha256(PROMPT.encode()).hexdigest(),
        "schema_sha256": _digest(SCHEMA),
        "cases_sha256": _digest(cases, canonical=True),
        "response_sha256": {key: _digest(value, canonical=True) for key, value in responses.items()},
    }


def _validate_response(stem, case, repeat, arm, response):
    key = case["key"]
    identity = response.get("identity")
    expected = {"key": key, "repeat": repeat, "arm": arm}
    if identity != stem and (
        not isinstance(identity, dict) or any(identity.get(name) != value for name, value in expected.items())
    ):
        raise ValueError(f"{stem}: неверная идентичность ответа")
    if (response.get("model"), response.get("effort")) != (MODEL, EFFORT):
        raise ValueError(f"{stem}: модель или effort расходится с протоколом")
    request = [{"key": key, "task": case["input"], "context": case["contexts"][arm]}]
    if response.get("request_sha256") != _digest(request):
        raise ValueError(f"{stem}: контрольная сумма входа не совпала")
    if response["state"] != "completed":
        if response.get("briefs"):
            raise ValueError(f"{stem}: незавершённый запрос содержит бриф")
        return
    if response["statuses"].keys() != {key}:
        raise ValueError(f"{stem}: первый ответ содержит неверный набор статусов")
    status = response["statuses"][key]
    if status not in RECEIVED | NOT_RECEIVED or response["briefs"].keys() != (
        {key} if status in RECEIVED else set()
    ):
        raise ValueError(f"{stem}: статус первого ответа не соответствует набору брифов")


def freeze_experiment(cases, responses, *, repeats=(1, 2)):
    """Зафиксировать запросы и первые ответы с отдельными SHA-256 на каждый запуск."""
    _validate_cases(cases)
    protocol = _protocol(cases, responses, repeats)
    _verify_jobs(cases, responses, repeats)
    return {"protocol": protocol, "cases": cases, "responses": responses}


def _verify_jobs(cases, responses, repeats):
    jobs = _jobs(cases, repeats)
    if jobs.keys() != responses.keys():
        raise ValueError("Архив должен перечислять все запланированные ответы, включая сбои")
    for stem, (case, repeat, arm) in jobs.items():
        _validate_response(stem, case, repeat, arm, responses[stem])
    return jobs


def _prepare_case(case, clone):
    key, revision = case["key"], case["base_sha"]
    try:
        rev_parse(str(clone), f"{revision}^{{commit}}")
        if "solution_sha" in case and rev_parse(str(clone), case["solution_sha"] + "^1") != revision:
            raise ValueError(f"{key}: исходный SHA не совпадает с первым родителем исправления")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{key}: Git-снимок недоступен в {clone}") from exc

    labels = [_labelled_paths(case, field) for field in ("obligatory", "read_only")]
    paths = set(_visible_lines(case["contexts"]["base"]))
    sources = {
        path: source
        for path in paths.union(*labels)
        if (source := file_at_ref(str(clone), path, revision)) is not None
    }
    if missing := paths.union(*labels) - sources.keys():
        raise ValueError(f"{key}: пути недоступны в Git-снимке: {sorted(missing)}")
    evidence, counts = {}, {}
    for arm in ARMS:
        context = case["contexts"][arm]
        eligible, details = check_brief_anchors(
            {"code": context}, lambda requested: {path: sources[path] for path in requested},
        )
        diagnostic = details.get("code", {})
        if (
            eligible.get("code") != _visible_lines(context)
            or diagnostic.get("mismatched") or diagnostic.get("unavailable")
        ):
            raise ValueError(f"{key} {arm}: показанный контекст расходится с Git")
        evidence[arm] = eligible
        counts[arm] = diagnostic["checked"]
    return labels, evidence, counts


def _score_brief(stem, answer, eligible, evidence):
    if answer is None:
        return set(), 0
    evidence_id = evidence.save_sections(eligible)
    refs = [
        BriefReferenceIn(section="code", path=path, line=line, reason=reason)
        for path, line, reason in parsed_references(answer["brief"])
    ]
    verified = evidence.verify_references(evidence_id, refs)
    if verified["status"] != "ok" or verified["render_status"] not in {"ok", "empty"}:
        raise ValueError(f"{stem}: ошибка штатной проверки ссылок")
    citations = verified["citations"]
    paths = {row["path"] for row in citations if row["status"] == "verified"}
    if paths != set(answer["verified_paths"]):
        raise ValueError(f"{stem}: архивные подтверждения расходятся со штатной проверкой")
    return paths, sum(row["status"] != "verified" for row in citations)


def _coverage(rows, field):
    groups = defaultdict(lambda: [0, 0])
    for row in rows:
        counts = groups[row["key"]]
        counts[0] += row[field]["hit"]
        counts[1] += row[field]["all"]
    hit = sum(pair[0] for pair in groups.values())
    total = sum(pair[1] for pair in groups.values())
    rates = [numerator / denominator for numerator, denominator in groups.values() if denominator]
    return {"hit": hit, "all": total}, (median(rates) if rates else None)


def _summarize(rows):
    core, task_median = _coverage(rows, "core")
    python, python_median = _coverage(rows, "python_core")
    deps, _ = _coverage(rows, "dependencies")
    return {
        "tasks": len({row["key"] for row in rows}),
        "briefs": len(rows),
        "missing_briefs": sum(not row["returned"] for row in rows),
        "core": core,
        "task_median_core": task_median,
        "python_core": python,
        "task_median_python_core": python_median,
        "dependencies": deps,
        "chars": sum(row["chars"] for row in rows),
        "verified_paths": sum(len(row["verified_paths"]) for row in rows),
        "labelled_paths": sum(row["labelled_paths"] for row in rows),
    }


def _group(rows, repositories):
    return {
        repo: {
            arm: {
                name: _summarize([
                    row for row in rows if row["arm"] == arm and (repo == "all" or row["repo"] == repo)
                    and (large is None or row["large"] == large)
                ])
                for name, large in (("all", None), ("large", True), ("ordinary", False))
            }
            for arm in ARMS
        }
        for repo in ("all", *repositories)
    }


def score_experiment(archive, clones, evidence):
    """Пересчитать ссылки и отдельно показать полные пары первых ответов."""
    cases, responses, protocol = (archive[key] for key in ("cases", "responses", "protocol"))
    repeats = protocol["repeats"]
    if protocol != _protocol(cases, responses, repeats):
        raise ValueError("Архив или версия промпта расходятся с контрольными суммами")
    _validate_cases(cases)
    jobs = _verify_jobs(cases, responses, repeats)
    clones = {REPO: ROOT, **clones}
    repositories = sorted({case.get("repo", REPO) for case in cases})
    if missing := set(repositories) - clones.keys():
        raise ValueError(f"Нужен --clone для {sorted(missing)}")
    prepared = {
        case["key"]: _prepare_case(case, clones[case.get("repo", REPO)]) for case in cases
    }

    rows = []
    for stem, (case, repeat, arm) in jobs.items():
        key, response = case["key"], responses[stem]
        (core, deps), eligible, counts = prepared[key]
        python = {path for path in core if path.endswith(".py") and not _is_test_path(path)}
        answer = response.get("briefs", {}).get(key)
        paths, rejected = _score_brief(stem, answer, eligible[arm], evidence)
        status = response.get("statuses", {}).get(key, response["state"])
        rows.append({
            "key": key,
            "repo": case.get("repo", REPO),
            "arm": arm,
            "repeat": repeat,
            "base_sha": case["base_sha"],
            "large": len(core) >= BULK_MIN,
            "status": status,
            "returned": answer is not None,
            "complete": answer is not None and not response["unexpected_keys"] and not response["unkeyed_records"],
            "core": {"hit": len(paths & core), "all": len(core)},
            "python_core": {"hit": len(paths & python), "all": len(python)},
            "dependencies": {"hit": len(paths & deps), "all": len(deps)},
            "verified_paths": sorted(paths),
            "core_paths": sorted(paths & core),
            "labelled_paths": len(paths & (core | deps)),
            "rejected_references": rejected,
            "chars": len(case["contexts"][arm]),
            "git_lines": counts[arm],
            "elapsed_s": response.get("elapsed_s"),
            "usage": response.get("usage"),
        })
    by_pair = defaultdict(list)
    for row in rows:
        by_pair[row["key"], row["repeat"]].append(row)
    paired = [row for pair in by_pair.values() if all(row["complete"] for row in pair) for row in pair]
    return {
        "scheduled": _group(rows, repositories),
        "complete_pairs": _group(paired, repositories),
        "per_brief": rows,
    }


def _clone_argument(raw):
    repo, sep, location = raw.partition("=")
    if not repo or not sep or not location:
        raise argparse.ArgumentTypeError("ожидается owner/repo=/локальный/путь")
    return repo, Path(location).expanduser().resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    pack = actions.add_parser("pack", help="Зафиксировать входы и захваченные первые ответы")
    pack.add_argument("--cases", type=Path, required=True)
    pack.add_argument("--responses", type=Path, required=True)
    pack.add_argument("--archive", type=Path, required=True)
    pack.add_argument("--repeats", nargs="+", type=int, default=(1, 2))
    score = actions.add_parser("score", help="Воспроизвести метрики без модели и сети")
    score.add_argument("--archive", type=Path, required=True)
    score.add_argument("--clone", action="append", type=_clone_argument, default=[])
    score.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "pack":
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        responses = json.loads(args.responses.read_text(encoding="utf-8"))
        archive = freeze_experiment(cases, responses, repeats=args.repeats)
        _write_bytes(args.archive, gzip.compress(json.dumps(archive, ensure_ascii=False).encode(), mtime=0))
    else:
        with gzip.open(args.archive, "rt", encoding="utf-8") as file:
            archive = json.load(file)
        with TemporaryDirectory(prefix="pri326-evidence-") as state:
            with patch.dict(os.environ, {"XDG_STATE_HOME": state}):
                evidence = BriefEvidence()
            report = score_experiment(archive, dict(args.clone), evidence)
        output = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            _write_bytes(args.output, (output + "\n").encode())
        else:
            print(output)


if __name__ == "__main__":
    main()
