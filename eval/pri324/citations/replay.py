"""Paired solve-task citation replay over immutable PRI-310 and PRI-299 packets."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory

from eval.pri299 import pilot
from eval.pri310 import replay
from reviewer.gitutil import file_at_ref
from reviewer.mcp.brief_anchors import check_brief_anchors
from reviewer.mcp.brief_evidence import _HEADER, _NUMBERED, BriefEvidence
from reviewer.mcp.schemas import BriefReferenceIn
from reviewer.metrics.brief_quality.briefs import RELEVANT_HEADER, parse_rendered_citations

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE_SHA = "740f9e871e14010a05800153f7f930e0e34d2f60"
CONTROL = pilot.MARKDOWN_PROMPT
ALIAS = CONTROL.replace(
    "- path:line — зачем читать или менять файл\n"
    "Указывай только пути из заголовков фрагментов и видимые номера строк.",
    "- @id — зачем читать или менять файл\n"
    "Скопируй только видимый @id из строки фрагмента, которая обосновывает пояснение. "
    "Символ @ обязателен.",
)
PROMPTS = {"control": CONTROL, "alias": ALIAS}
SCHEMA = pilot.load_conditions()["markdown"][1]
REPEATS = (1, 2)
ARCHIVE = "responses.json.gz"
ALIAS_LABEL = re.compile(r"`?(@[0-9]{3})`?")


@cache
def source(sha: str, path: str) -> str | None:
    return file_at_ref(str(ROOT), path, sha)


@cache
def verified_rows(sha: str, context: str) -> dict[str, set[int]]:
    eligible, diagnostics = check_brief_anchors(
        {"code": context},
        lambda paths: {path: body for path in paths if (body := source(sha, path)) is not None},
    )
    if not eligible or any(
        counts["mismatched"] or counts["unavailable"] for counts in diagnostics.values()
    ):
        raise ValueError(f"Секция не прошла сверку с Git-снимком {sha}")
    return eligible["code"]


def alias_context(
    context: str, verified: dict[str, set[int]]
) -> tuple[str, dict[str, tuple[str, int]]]:
    """Replace only coordinate prefixes. Every alias round-trips to a verified row."""
    lines = []
    anchors: dict[str, tuple[str, int]] = {}
    path = None
    for line in context.split("\n"):
        if header := _HEADER.fullmatch(line):
            path = header["path"]
        elif line:
            row = _NUMBERED.fullmatch(line)
            if row is None or path is None or (number := int(row[1])) not in verified.get(path, set()):
                raise ValueError("В секции есть непроверенная или неразбираемая строка")
            label = f"@{len(anchors) + 1:03}"
            width = len(line.partition("|")[0]) - 1
            if len(label) != 4 or len(label) > width:
                raise ValueError("Идентификатор не помещается в поле координаты")
            anchors[label] = (path, number)
            line = f"{label:>{width}} | {row[2]}"
        lines.append(line)
    expected = {(path, number) for path, ns in verified.items() for number in ns}
    if len(anchors) != len(expected) or set(anchors.values()) != expected:
        raise ValueError("Преобразование изменило состав подтверждённых строк")
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) != len(context.encode("utf-8")):
        raise ValueError("Преобразование изменило байтовую длину секции")
    return rendered, anchors


def inputs() -> tuple[dict, dict]:
    main, labels = replay.load_inputs()
    main = [row for row in main if len(labels[row["key"]]["core"]) >= replay.MIN_LARGE_FILES] + [
        row for row in main if row["key"] in {"PRI-228", "PRI-256"}
    ]
    main = [{**row, "context": row["contexts"]["rank"]} for row in main]
    corpus = replay.read_json(HERE.parent.parent / "pri278/independent-corpus.json")
    independent = {case["key"]: case for case in corpus}
    pilot_inputs = json.loads(pilot.load_input())
    for row in pilot_inputs:
        case = independent[row["key"]]
        row["base_sha"] = case["base_sha"]
        labels[row["key"]] = {
            name: {item["path"] for item in case[field]} for name, field in replay.LABEL_GROUPS.items()
        }
    return {"main": [main[:4], main[4:]], "independent": [pilot_inputs[:3], pilot_inputs[3:]]}, labels


def request_text(batch: list[dict], mode: str) -> str:
    result = []
    for row in batch:
        context = row["context"]
        if mode == "alias":
            context, _ = alias_context(context, verified_rows(row["base_sha"], context))
        result.append({"key": row["key"], "task": row["task"], "context": context})
    return json.dumps(result, ensure_ascii=False)


def metadata(groups: dict) -> dict:
    return {
        "base_sha": BASE_SHA,
        "model": pilot.MODEL,
        "reasoning_effort": pilot.EFFORT,
        "schema_sha256": pilot.digest(json.dumps(SCHEMA, ensure_ascii=False)),
        "prompt_sha256": {mode: pilot.digest(prompt) for mode, prompt in PROMPTS.items()},
        "batches": {
            cohort: [{row["key"]: row["base_sha"] for row in batch} for batch in batches]
            for cohort, batches in groups.items()
        },
    }


def load_archive(work: Path) -> dict:
    return json.loads(gzip.decompress((work / ARCHIVE).read_bytes()))


def write_archive(work: Path, archive: dict) -> None:
    text = json.dumps(archive, ensure_ascii=False, separators=(",", ":"))
    path = work / ARCHIVE
    pending = path.with_suffix(".tmp")
    pending.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    pending.replace(path)


def run(groups: dict, work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    if (work / ARCHIVE).exists():
        raise FileExistsError(work / ARCHIVE)
    protocol = metadata(groups)
    protocol["codex_version"] = subprocess.check_output(["codex", "--version"], text=True).strip()
    with TemporaryDirectory(prefix="pri324-citations-") as directory:
        schema = Path(directory) / "schema.json"
        schema.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
        records, jobs = {}, {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            for rep in REPEATS:
                for cohort, batches in groups.items():
                    for index, batch in enumerate(batches, 1):
                        modes = PROMPTS if (rep + index) % 2 else reversed(PROMPTS)
                        for mode in modes:
                            condition = f"{cohort}-b{index}-{mode}"
                            request = request_text(batch, mode)
                            task = executor.submit(
                                pilot.run_condition, condition, rep, request, PROMPTS[mode], schema,
                            )
                            jobs[f"{condition}-{rep}"] = (task, pilot.digest(request))
            for key, (task, checksum) in jobs.items():
                records[key] = {**task.result(), "input_sha256": checksum}
                write_archive(work, {"protocol": protocol, "records": records})


def parse_answer(
    brief: dict, aliases: dict[str, tuple[str, int]] | None,
) -> tuple[list[tuple[str, int, str]], int]:
    if brief.keys() != {"key", "brief"} or not isinstance(brief["brief"], str):
        return [], 1
    lines = [line.strip() for line in brief["brief"].splitlines()]
    headings = [i for i, line in enumerate(lines) if pilot.MARKDOWN_HEADING.fullmatch(line)]
    if len(headings) != 1:
        return [], 1
    parsed = []
    invalid = 0
    for line in lines[headings[0] + 1:]:
        if not line or pilot.DROPPED_NOTE.fullmatch(line):
            continue
        item = pilot.MARKDOWN_REF.fullmatch(line)
        if item is None:
            invalid += 1
            continue
        if pilot.DROPPED_REASON.match(item["reason"]):
            continue
        coordinate = item["code"] or item["bare"]
        if aliases is not None:
            match = ALIAS_LABEL.fullmatch(coordinate)
            anchor = aliases.get(match[1]) if match else None
        else:
            match = pilot.PATH_LINE.fullmatch(coordinate)
            anchor = (match[1], int(match[2])) if match else None
        if anchor is None:
            invalid += 1
        else:
            parsed.append((*anchor, item["reason"]))
    return parsed, invalid


def score_case(row: dict, mode: str, brief: dict | None, gold: dict, verifier: BriefEvidence) -> dict:
    context = row["context"]
    visible = verified_rows(row["base_sha"], context)
    aliases = alias_context(context, visible)[1] if mode == "alias" else None
    parsed, invalid = parse_answer(brief, aliases) if brief else ([], 0)
    refs = [
        BriefReferenceIn(section="code", path=path, line=line, reason=reason)
        for path, line, reason in parsed
    ]
    evidence_id = verifier.save_sections({"code": visible})
    if evidence_id is None:
        raise ValueError(f"Не удалось записать подтверждённые строки {row['key']}")
    result = verifier.verify_references(evidence_id, refs)
    if result["status"] != "ok" or result["render_status"] not in {"ok", "empty"}:
        raise ValueError(f"Не удалось проверить ответ {row['key']}")
    citations = parse_rendered_citations(result["markdown"], RELEVANT_HEADER)
    if citations is None:
        raise ValueError(f"Не удалось разобрать подтверждённые ссылки {row['key']}")
    accepted = {path for path, _ in citations}
    return {
        "key": row["key"],
        "base_sha": row["base_sha"],
        "large": len(gold["core"]) >= replay.MIN_LARGE_FILES,
        "missing": brief is None,
        "citations": citations,
        "invalid": invalid + len(parsed) - len(citations),
        "core": sorted(accepted & gold["core"]),
        "dependencies": sorted(accepted & gold["dependencies"]),
        "unlabelled": sorted(accepted - gold["core"] - gold["dependencies"]),
        "expected_core": len(gold["core"]),
        "expected_dependencies": len(gold["dependencies"]),
    }


def aggregate(rows: list[dict]) -> dict:
    total = Counter()
    for row in rows:
        total.update({
            "core": len(row["core"]),
            "dependencies": len(row["dependencies"]),
            "unlabelled": len(row["unlabelled"]),
            "core_total": row["expected_core"],
            "dependencies_total": row["expected_dependencies"],
            "invalid": row["invalid"],
            "missing": row["missing"],
            "verified_citations": len(row["citations"]),
        })
    return dict(total)


def score(groups: dict, labels: dict, archive: dict) -> dict:
    if any(archive["protocol"].get(key) != value for key, value in metadata(groups).items()):
        raise ValueError("Архив не соответствует протоколу")
    verifier = BriefEvidence()
    result = {}
    for cohort, batches in groups.items():
        result[cohort] = {}
        for mode in PROMPTS:
            tasks = []
            usage = Counter()
            for rep in REPEATS:
                for index, batch in enumerate(batches, 1):
                    condition = f"{cohort}-b{index}-{mode}"
                    record = archive["records"][f"{condition}-{rep}"]
                    expected_input = pilot.digest(request_text(batch, mode))
                    if (record.get("rep"), record.get("condition"), record.get("input_sha256")) != (
                        rep, condition, expected_input
                    ):
                        raise ValueError(f"Архив не соответствует входу {condition}-{rep}")
                    observed = pilot.index_response(record["response"])
                    usage.update(record["usage"])
                    for row in batch:
                        task = score_case(
                            row, mode, observed.get(row["key"]), labels[row["key"]], verifier,
                        )
                        tasks.append({"rep": rep, **task})
            result[cohort][mode] = {
                "large": aggregate([row for row in tasks if row["large"]]),
                "ordinary": aggregate([row for row in tasks if not row["large"]]),
                "usage": dict(usage),
                "tasks": tasks,
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "score"])
    parser.add_argument("--work", type=Path, default=HERE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    groups, labels = inputs()
    if args.action == "run":
        if args.work.resolve() == HERE:
            parser.error("Для нового прогона укажите отдельный --work")
        run(groups, args.work.resolve())
    with TemporaryDirectory(prefix="pri324-brief-evidence-") as directory:
        existing = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = directory
        try:
            result = score(groups, labels, load_archive(args.work.resolve()))
        finally:
            if existing is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = existing
    if args.output:
        replay.write_json(args.output, result)
    print(json.dumps({
        cohort: {mode: {k: v for k, v in row.items() if k != "tasks"} for mode, row in modes.items()}
        for cohort, modes in result.items()
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
