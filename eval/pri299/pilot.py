"""Парный пилот PRI-299 на замороженных данных PRI-278."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import runpy
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

HERE = Path(__file__).parent
TASKS = ("PRI-170", "PRI-202", "PRI-221", "PRI-179", "PRI-205", "PRI-207")
INPUT_SHA = "4a9773cef65ef00d0bda3b1404337f5d035b7e1687a76d2c3dd32bcf087b8157"
MARKDOWN_PROMPT_SHA = "9777c6d0559037b6a35960fa520e87117aeb12cef9428e875840b2e03f9264f3"
MODEL = "gpt-6-luna"
EFFORT = "low"
MAX_FILES = 20
ORDERS = (("markdown", "structured"), ("structured", "markdown"))
STRING = {"type": "string"}
MARKDOWN_PROMPT = """Собери отдельный краткий бриф для каждой задачи. Используй только постановку
и переданную секцию кода этой задачи. Не вызывай инструменты. Другие задачи в
пакете независимы. Одна строка с сутью задачи и раздел Relevant code.
В Relevant code сохрани каждый файл, который непосредственно нужен для
изменения или понимания реализации. Сам определяй релевантность без потолка
на число файлов. Один пункт на файл в порядке выдачи. Формат пункта
- path:line — зачем читать или менять файл
Указывай только пути из заголовков фрагментов и видимые номера строк.
Заверши раздел строкой (dropped N: краткая причина).
Верни JSON заданной схемы, поле brief содержит Markdown.
"""
MARKDOWN_HEADING = re.compile(
    r"(?:#{1,6}\s+)?(?:Relevant code:?|\*\*Relevant code:?\*\*:?)",
    re.IGNORECASE,
)
MARKDOWN_REF = re.compile(
    r"-\s+(?:`(?P<code>[^`]+)`|(?P<bare>[^\s`]+))\s+[—–-]\s+(?P<reason>\S.*)",
)
PATH_LINE = re.compile(r"(.+):([1-9][0-9]*)")
DROPPED_NOTE = re.compile(r"(?:-\s*)?\(dropped\b.*\)", re.IGNORECASE)
DROPPED_REASON = re.compile(r"(?:отброшено|исключено|dropped|excluded)\s*:", re.IGNORECASE)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_pri278_input(pri278: Path) -> str:
    # Разметка корпуса не входит ни в запрос, ни в JSON-схему ответа.
    tasks = {case["key"]: case["input"] for case in read_json(pri278 / "independent-corpus.json")}
    saved = {}
    with gzip.open(pri278 / "independent/evidence.jsonl.gz", "rt", encoding="utf-8") as source:
        for row in map(json.loads, source):
            if row.get("variant") == "all-30" and "retrieval" in row and row["key"] in TASKS:
                context = row["retrieval"]["modes"]["hybrid"]
                if len(set(context["context_paths"])) > MAX_FILES:
                    raise ValueError(f"Превышен файловый лимит для {row['key']}")
                if row["key"] in saved:
                    raise ValueError(f"Дублируется замороженная выдача для {row['key']}")
                saved[row["key"]] = context["context"]
    batch = [{"key": key, "task": tasks[key], "context": saved[key]} for key in TASKS]
    return json.dumps(batch, ensure_ascii=False)


def load_input() -> str:
    serialized = gzip.decompress((HERE / "input.json.gz").read_bytes()).decode("utf-8")
    if digest(serialized) != INPUT_SHA:
        raise ValueError(f"Изменился вход пилота: {digest(serialized)}")
    return serialized


def object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def load_conditions() -> dict[str, tuple[str, dict]]:
    if digest(MARKDOWN_PROMPT) != MARKDOWN_PROMPT_SHA:
        raise ValueError("Изменился контрольный промпт")

    structured = (
        MARKDOWN_PROMPT.replace(
            "пакете независимы. Одна строка с сутью задачи и раздел Relevant code.",
            "пакете независимы. Одна строка с сутью задачи (поле summary) и записи Relevant code.",
        )
        .replace(
            "на число файлов. Один пункт на файл в порядке выдачи. Формат пункта\n"
            "- path:line — зачем читать или менять файл",
            "на число файлов. Одна запись на файл в порядке выдачи. Формат записи\n"
            "section=code, path, line, reason — зачем читать или менять файл",
        )
        .replace(
            "Заверши раздел строкой (dropped N: краткая причина).\n"
            "Верни JSON заданной схемы, поле brief содержит Markdown.",
            "Укажи строку (dropped N: краткая причина) в поле dropped.\n"
            "Верни JSON заданной схемы, поле references содержит записи.",
        )
    )
    reference = object_schema(
        {
            "section": {"type": "string", "enum": ["code"]},
            "path": STRING,
            "line": {"type": "integer"},
            "reason": STRING,
        }
    )
    brief = object_schema(
        {
            "key": STRING,
            "summary": STRING,
            "references": {"type": "array", "items": reference},
            "dropped": STRING,
        }
    )
    return {
        "markdown": (
            MARKDOWN_PROMPT,
            object_schema(
                {
                    "briefs": {
                        "type": "array",
                        "items": object_schema({"key": STRING, "brief": STRING}),
                    },
                }
            ),
        ),
        "structured": (structured, object_schema({"briefs": {"type": "array", "items": brief}})),
    }


def verify_source(pri278: Path, input_text: str, *, check_labels: bool) -> None:
    if load_pri278_input(pri278) != input_text:
        raise ValueError("Сохранённый вход не совпадает с исходным архивом PRI-278")
    previous = runpy.run_path(str(pri278 / "briefs.py"))
    if load_conditions()["markdown"] != (previous["PROMPT"], previous["SCHEMA"]):
        raise ValueError("Промпт или JSON-схема контроля не совпали с PRI-278")
    if check_labels:
        cases = {case["key"]: case for case in read_json(pri278 / "independent-corpus.json")}
        original_labels = {
            key: {
                "core": sorted({row["path"] for row in cases[key]["obligatory"]}),
                "dependencies": sorted({row["path"] for row in cases[key]["read_only"]}),
            }
            for key in TASKS
        }
        if original_labels != read_json(HERE / "labels.json"):
            raise ValueError("Разметка не совпала с PRI-278")


def run_condition(
    name: str,
    rep: int,
    input_text: str,
    prompt: str,
    schema_path: Path,
) -> dict:
    command = [
        "codex",
        "exec",
        "--cd",
        str(schema_path.parent),
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "-m",
        MODEL,
        "-c",
        f'model_reasoning_effort="{EFFORT}"',
        "-c",
        "mcp_servers={}",
        "--output-schema",
        str(schema_path),
        "-",
    ]
    print(f"Запуск {name}, повтор {rep}", flush=True)
    start = time.perf_counter()
    result = subprocess.run(
        command,
        input=prompt + "\n" + input_text,
        capture_output=True,
        encoding="utf-8",
        timeout=540,
    )
    duration = round(time.perf_counter() - start, 3)
    if result.returncode:
        raise RuntimeError(f"{name}-{rep}, код {result.returncode}: {result.stderr}")
    events = [json.loads(line) for line in result.stdout.splitlines()]
    items = [event["item"] for event in events if event["type"] == "item.completed"]
    if unexpected := [
        item["type"] for item in items if item["type"] not in {"agent_message", "reasoning"}
    ]:
        raise ValueError(f"{name}-{rep}, неожиданные действия: {unexpected}")
    messages = [item["text"] for item in items if item["type"] == "agent_message"]
    usage = [event["usage"] for event in events if event["type"] == "turn.completed"][-1]
    print(f"Готов {name}, повтор {rep}, {duration} с", flush=True)
    return {
        "condition": name,
        "rep": rep,
        "elapsed_s": duration,
        "usage": usage,
        "response": messages[-1] if messages else "",
    }


def protocol_metadata(conditions: dict) -> dict:
    return {
        "input_sha256": INPUT_SHA,
        "prompt_sha256": {name: digest(prompt) for name, (prompt, _) in conditions.items()},
        "schema_sha256": {
            name: digest(json.dumps(schema, ensure_ascii=False))
            for name, (_, schema) in conditions.items()
        },
        "tasks": list(TASKS),
        "file_limit": MAX_FILES,
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "orders": [list(order) for order in ORDERS],
    }


def run(input_text: str, work: Path) -> None:
    conditions = load_conditions()
    work.mkdir(parents=True, exist_ok=True)
    output = work / "responses.json"
    if output.exists():
        raise FileExistsError(output)
    metadata = {
        **protocol_metadata(conditions),
        "codex_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
        "runs": [],
    }
    print(f"Вход {INPUT_SHA}, задачи {', '.join(TASKS)}", flush=True)
    with TemporaryDirectory(prefix="pri299-codex-") as tmp:
        invocation = Path(tmp)
        for name, (_, schema) in conditions.items():
            (invocation / f"{name}.json").write_text(
                json.dumps(schema, ensure_ascii=False),
                encoding="utf-8",
            )
        for rep, order in enumerate(ORDERS, 1):
            with ThreadPoolExecutor(max_workers=2) as executor:
                jobs = [
                    executor.submit(
                        run_condition,
                        name,
                        rep,
                        input_text,
                        conditions[name][0],
                        invocation / f"{name}.json",
                    )
                    for name in order
                ]
                for job in jobs:
                    metadata["runs"].append(job.result())
                    write_json(output, metadata)


def parse_markdown(text: str) -> tuple[list[tuple[str, int]], list[str]]:
    """Прочитать ссылки из указанной в исходном промпте секции Markdown."""
    lines = [line.strip() for line in text.splitlines()]
    headings = [index for index, line in enumerate(lines) if MARKDOWN_HEADING.fullmatch(line)]
    if len(headings) != 1:
        return [], ["code_heading_missing_or_repeated"]
    refs = []
    errors = []
    for line in lines[headings[0] + 1 :]:
        if not line or DROPPED_NOTE.fullmatch(line):
            continue
        item = MARKDOWN_REF.fullmatch(line)
        position = PATH_LINE.fullmatch(item["code"] or item["bare"]) if item else None
        if not position:
            errors.append("citation_syntax")
        elif not DROPPED_REASON.match(item["reason"]):
            refs.append((position[1], int(position[2])))
    return refs, errors


def index_response(response: str) -> dict:
    try:
        value = json.loads(response)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict) or value.keys() != {"briefs"}:
        return {}
    if not isinstance(value["briefs"], list):
        return {}
    indexed = {}
    for row in value["briefs"]:
        if isinstance(row, dict) and isinstance(row.get("key"), str):
            key = row["key"]
            indexed[key] = row if key not in indexed else None
    return indexed


def score_markdown(brief: dict, visible: dict[str, set[int]]) -> tuple:
    from reviewer.metrics.brief_quality.briefs import RELEVANT_HEADER, has_section

    if brief.keys() != {"key", "brief"} or not isinstance(brief["brief"], str):
        return [], [], ["brief_schema"], False
    text = brief["brief"]
    candidates, errors = parse_markdown(text)
    checked = [
        "path_unseen"
        if path not in visible
        else "verified"
        if line in visible[path]
        else "outside_excerpt"
        for path, line in candidates
    ]
    grounded = [
        ref for ref, status in zip(candidates, checked, strict=True) if status == "verified"
    ]
    return grounded, checked, errors, has_section(text, RELEVANT_HEADER)


def score_structured(brief: dict, context: str) -> tuple:
    from pydantic import TypeAdapter, ValidationError

    from reviewer.mcp.brief_evidence import BriefEvidence
    from reviewer.mcp.schemas import BriefReferenceIn
    from reviewer.metrics.brief_quality.briefs import RELEVANT_HEADER, parse_rendered_citations

    if brief.keys() != {"key", "summary", "references", "dropped"} or not all(
        isinstance(brief[name], str) for name in ("summary", "dropped")
    ):
        return [], [], ["brief_schema"], False
    records = brief["references"]
    if not isinstance(records, list) or any(
        not isinstance(record, dict)
        or record.keys() != {"section", "path", "line", "reason"}
        or record["section"] != "code"
        for record in records
    ):
        return [], [], ["reference_schema"], False
    try:
        references = TypeAdapter(list[BriefReferenceIn]).validate_python(records, strict=True)
    except ValidationError:
        return [], [], ["reference_schema"], False
    evidence = BriefEvidence()
    evidence_id = evidence.save({"code": context})
    if evidence_id is None:
        raise ValueError("В замороженной выдаче нет ни одной видимой строки")
    result = evidence.verify_references(evidence_id, references)
    if result["status"] != "ok":
        raise ValueError(f"Выдача недоступна: {result['status']}")
    checked = [row["status"] for row in result["citations"]]
    if result["render_status"] not in {"ok", "empty"}:
        return [], checked, [result["render_status"]], False
    try:
        parsed = parse_rendered_citations(result["markdown"], RELEVANT_HEADER)
    except ValueError:
        return [], checked, ["rendered_markdown_unreadable"], False
    if parsed is None:
        return [], checked, ["rendered_code_heading_missing"], False
    return parsed, checked, [], True


def score_run(batch: list[dict], labels: dict, run_result: dict) -> dict:
    from reviewer.mcp.brief_evidence import _visible_lines

    condition = run_result["condition"]
    observed = index_response(run_result["response"])
    rows = []
    for item in batch:
        key = item["key"]
        visible = _visible_lines(item["context"])
        if len(visible) > MAX_FILES:
            raise ValueError(f"Превышен файловый лимит для {key}")
        brief = observed.get(key)
        if brief is None:
            ground, checked, errors, canonical = [], [], ["brief_missing_or_repeated"], False
        elif condition == "markdown":
            ground, checked, errors, canonical = score_markdown(brief, visible)
        else:
            ground, checked, errors, canonical = score_structured(brief, item["context"])
        paths = {path for path, _ in ground}
        gold = {group: set(labels[key][group]) for group in ("core", "dependencies")}
        rows.append(
            {
                "key": key,
                "coverage": {
                    group: {
                        "matched": len(paths & expected),
                        "total": len(expected),
                        "visible": len(visible.keys() & expected),
                    }
                    for group, expected in gold.items()
                },
                "off_label_paths": sorted(paths - gold["core"] - gold["dependencies"]),
                "counted_citations": ground,
                "reference_statuses": {
                    status: checked.count(status) for status in sorted(set(checked))
                },
                "format_errors": sorted(set(errors)),
                "canonical_code_heading": canonical,
            }
        )
    return {
        "condition": condition,
        "rep": run_result["rep"],
        "aggregate": {
            group: {
                "matched": sum(row["coverage"][group]["matched"] for row in rows),
                "total": sum(row["coverage"][group]["total"] for row in rows),
                "median_recall": median(
                    row["coverage"][group]["matched"] / row["coverage"][group]["total"]
                    for row in rows
                    if row["coverage"][group]["total"]
                ),
            }
            for group in ("core", "dependencies")
        },
        "format_error_briefs": sum(bool(row["format_errors"]) for row in rows),
        "format_error_denominator": len(rows),
        "missing_canonical_heading_briefs": sum(not row["canonical_code_heading"] for row in rows),
        "checked_references": sum(sum(row["reference_statuses"].values()) for row in rows),
        "rejected_references": sum(
            count
            for row in rows
            for status, count in row["reference_statuses"].items()
            if status != "verified"
        ),
        "tasks": rows,
    }


def score(input_text: str, work: Path) -> None:
    records = read_json(work / "responses.json")
    for field, expected in protocol_metadata(load_conditions()).items():
        if records[field] != expected:
            raise ValueError(f"Ответы не соответствуют протоколу: {field}")
    runs = records["runs"]
    expected_order = [(name, rep) for rep, order in enumerate(ORDERS, 1) for name in order]
    if [(row["condition"], row["rep"]) for row in runs] != expected_order:
        raise ValueError("Требуются два полных парных повтора в порядке протокола")
    labels = read_json(HERE / "labels.json")
    batch = json.loads(input_text)
    results = [score_run(batch, labels, record) for record in runs]
    write_json(work / "metrics.json", {"input_sha256": INPUT_SHA, "runs": results})
    for report in results:
        print(
            report["rep"],
            report["condition"],
            report["aggregate"],
            f"format={report['format_error_briefs']}/{report['format_error_denominator']}",
            f"rejected={report['rejected_references']}/{report['checked_references']}",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "score"])
    parser.add_argument("--pri278", type=Path, help="сверить вложенные данные с исходным PRI-278")
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    input_text = load_input()
    if args.pri278 is not None:
        verify_source(args.pri278.resolve(), input_text, check_labels=args.action == "score")
    action = run if args.action == "run" else score
    action(input_text, args.work.resolve())


if __name__ == "__main__":
    main()
