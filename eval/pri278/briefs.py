"""Use Luna to distill the rendered code section without access to gold labels."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from reviewer.mcp.brief_evidence import _HEADER, _TRUNCATED, _visible_lines

MODEL = "gpt-6-luna"
COVERAGE_VERSION = "visible-lines-v2"
CITATION_STATUSES = ("verified", "path_unseen", "outside_excerpt")
ANCHOR = re.compile(r"^//\s+\S+\s+\(([^()]+):(\d+)-(\d+)\)$", re.MULTILINE)
REFERENCE = re.compile(r"^- `?([^\s`]+):(\d+)`?[^\S\n]+[—–-][^\S\n]+(\S[^\n]*)", re.MULTILINE)
REJECTED = re.compile(r"^(?:отброшено|исключено|dropped|excluded)\s*:", re.IGNORECASE)
PROMPT = """Собери отдельный краткий бриф для каждой задачи. Используй только постановку
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
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["briefs"],
    "properties": {
        "briefs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "brief"],
                "properties": {"key": {"type": "string"}, "brief": {"type": "string"}},
            },
        },
    },
}


def parsed_references(text):
    for path, line, reason in REFERENCE.findall(text):
        if not REJECTED.match(reason):
            yield path, int(line), reason


def check_references(text, context):
    """Check each cited path and line once against the exact rendered section."""
    visible = _visible_lines(context)
    known_paths = set(visible)
    for line in context.split("\n"):
        if line == _TRUNCATED:
            break
        if header := _HEADER.fullmatch(line):
            known_paths.add(header["path"])

    checked = {}
    for path, line, reason in parsed_references(text):
        if (path, line) in checked:
            continue
        if line in visible.get(path, ()):
            status = "verified"
        elif path in known_paths:
            status = "outside_excerpt"
        else:
            status = "path_unseen"
        checked[path, line] = {"path": path, "line": line, "reason": reason, "status": status}
    return list(checked.values())


def verified_paths(citations):
    return {item["path"] for item in citations if item["status"] == "verified"}


def citation_counts(citations):
    counts = Counter(item["status"] for item in citations)
    return {status: counts[status] for status in CITATION_STATUSES}


def check_briefs(briefs, batch):
    by_key = {row["key"]: row["brief"] for row in briefs}
    if len(by_key) != len(briefs) or set(by_key) != {row["key"] for row in batch}:
        raise ValueError("Luna omitted or duplicated tasks")
    for row in batch:
        text, context = by_key[row["key"]], row["context"]
        checked = check_references(text, context)
        # Keep the original archive fields for path-only replay and the legacy diagnostic.
        bounds = {path: (int(first), int(last)) for path, first, last in ANCHOR.findall(context)}
        references = [(path, line) for path, line, _ in parsed_references(text)]
        yield {
            "key": row["key"], "brief": text, "paths": [path for path, _ in references],
            "invalid_references": [
                [path, line] for path, line in references
                if path not in bounds or not bounds[path][0] <= line <= bounds[path][1]
            ],
            "brief_coverage_version": COVERAGE_VERSION,
            "verified_paths": sorted(verified_paths(checked)),
            "citation_counts": citation_counts(checked),
        }


def run_batch(batch, invocation, output, *, prompt=PROMPT):
    input_text = json.dumps(batch, ensure_ascii=False)
    schema_path = invocation / "schema.json"
    command = [
        "codex", "exec", "--cd", str(invocation), "--skip-git-repo-check", "--ephemeral",
        "--json", "--sandbox", "read-only", "-m", MODEL, "-c", 'model_reasoning_effort="low"',
        "-c", "mcp_servers={}", "--output-schema", str(schema_path), "-",
    ]
    start = time.perf_counter()
    result = subprocess.run(
        command, input=prompt + "\n" + input_text, capture_output=True, text=True, timeout=540,
    )
    duration = round(time.perf_counter() - start, 3)
    output.with_suffix(".events.jsonl").write_text(result.stdout)
    output.with_suffix(".stderr").write_text(result.stderr)
    result.check_returncode()
    events = [json.loads(line) for line in result.stdout.splitlines()]
    messages = [e["item"]["text"] for e in events
                if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
    tool_calls = [e["item"]["type"] for e in events
                  if e["type"] == "item.completed"
                  and e["item"]["type"] not in {"agent_message", "reasoning"}]
    if tool_calls:
        raise ValueError(f"Unexpected tool or action: {tool_calls}")
    usage = [e["usage"] for e in events if e["type"] == "turn.completed"][-1]
    observed = json.loads(messages[-1])["briefs"]
    output.write_text(json.dumps({
        "model": MODEL, "reasoning_effort": "low", "batch_elapsed_s": duration, "usage": usage,
        "briefs": list(check_briefs(observed, batch)),
    }, ensure_ascii=False, indent=2) + "\n")
    print(output.name, len(batch), usage, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=Path(__file__).with_name("corpus.json"))
    parser.add_argument("--input", nargs="+", required=True)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--wait", type=int, default=1800)
    args = parser.parse_args()
    work = args.work.resolve()
    invocation = work / "luna-invocation"
    invocation.mkdir(exist_ok=True)
    (invocation / "schema.json").write_text(json.dumps(SCHEMA))
    cases = json.loads(args.corpus.read_text())
    for label in args.input:
        split = label.split("-")[0]
        rows = [case for case in cases if split in ("all", case["split"])]
        batches = [rows[i:i + args.batch_size] for i in range(0, len(rows), args.batch_size)]
        output = work / f"luna-{label}"
        output.mkdir(exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            jobs = {}
            pending = dict(enumerate(batches, 1))
            deadline = time.monotonic() + args.wait
            while pending and time.monotonic() < deadline:
                for index, batch in list(pending.items()):
                    target = output / f"batch-{index}.json"
                    inputs = [work / label / f"{row['key']}.json" for row in batch]
                    if target.exists():
                        del pending[index]
                    elif all(p.exists() for p in inputs):
                        items = [
                            {"key": row["key"], "task": row["input"],
                             "context": json.loads(path.read_text())["modes"]["hybrid"]["context"]}
                            for row, path in zip(batch, inputs, strict=True)
                        ]
                        jobs[index] = executor.submit(run_batch, items, invocation, target)
                        del pending[index]
                if pending:
                    time.sleep(3)
            errors = {}
            for index, job in jobs.items():
                try:
                    job.result()
                except Exception as error:
                    errors[index] = type(error).__name__
            if errors or pending:
                raise RuntimeError(f"Failed batches {errors}; waiting for retrieval {list(pending)}")


if __name__ == "__main__":
    main()
