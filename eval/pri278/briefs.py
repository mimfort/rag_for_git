"""Use Luna to distill the rendered code section without access to gold labels."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from reviewer.mcp.brief_evidence import _HEADER, _TRUNCATED, _visible_lines

MODEL = "gpt-6-luna"
EFFORT = "low"
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
        yield _checked_brief(row, by_key[row["key"]])


def _checked_brief(row, text):
    context = row["context"]
    checked = check_references(text, context)
    # Keep the original archive fields for path-only replay and the legacy diagnostic.
    bounds = {path: (int(first), int(last)) for path, first, last in ANCHOR.findall(context)}
    references = [(path, line) for path, line, _ in parsed_references(text)]
    return {
        "key": row["key"], "brief": text, "paths": [path for path, _ in references],
        "invalid_references": [
            [path, line] for path, line in references
            if path not in bounds or not bounds[path][0] <= line <= bounds[path][1]
        ],
        "brief_coverage_version": COVERAGE_VERSION,
        "verified_paths": sorted(verified_paths(checked)),
        "citation_counts": citation_counts(checked),
    }


def _invoke_batch(batch, invocation, output, prompt, schema_path):
    input_text = json.dumps(batch, ensure_ascii=False)
    command = [
        "codex", "exec", "--cd", str(invocation), "--skip-git-repo-check", "--ephemeral",
        "--json", "--sandbox", "read-only", "-m", MODEL, "-c",
        f'model_reasoning_effort="{EFFORT}"',
        "-c", "mcp_servers={}", "--output-schema", str(schema_path), "-",
    ]
    start = time.perf_counter()
    result = subprocess.run(
        command, input=prompt + "\n" + input_text,
        check=False, capture_output=True, text=True, timeout=540,
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
    if not isinstance(observed, list):
        raise TypeError("Luna returned a non-array briefs field")
    return {
        "state": "completed",
        "elapsed_s": duration,
        "usage": usage,
        **_classify_attempt(observed, batch),
    }


def _classify_attempt(observed, batch):
    expected = {row["key"]: row for row in batch}
    rows = {key: [] for key in expected}
    unexpected = Counter()
    unkeyed = 0
    for record in observed:
        if not isinstance(record, dict) or not isinstance(record.get("key"), str):
            unkeyed += 1
            continue
        key = record["key"]
        if key in rows:
            rows[key].append(record)
        else:
            unexpected[key] += 1

    statuses, accepted = {}, {}
    for key, records in rows.items():
        if not records:
            statuses[key] = "missing"
        elif len(records) > 1:
            statuses[key] = "duplicate"
        elif records[0].keys() != {"key", "brief"} or not isinstance(records[0]["brief"], str):
            statuses[key] = "invalid"
        elif not records[0]["brief"].strip():
            statuses[key] = "blank"
        else:
            brief = _checked_brief(expected[key], records[0]["brief"])
            accepted[key] = brief
            statuses[key] = "verified" if brief["verified_paths"] else "no_verified_refs"
    return {
        "statuses": statuses,
        "briefs": accepted,
        "unexpected_keys": dict(unexpected),
        "unkeyed_records": unkeyed,
    }


def _write_bytes(path, contents):
    with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            temp.write(contents)
            temp.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _write_json(path, payload):
    _write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def _load_matching(path, fingerprint):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("input_sha256") != fingerprint:
        raise ValueError(f"Файл {path} относится к другому входу или создан без отпечатка")
    return payload


def _coverage(statuses, unexpected_keys, unkeyed_records):
    counts = Counter(statuses.values())
    return {
        "expected": len(statuses),
        "returned": counts["verified"] + counts["no_verified_refs"],
        "with_verified_refs": counts["verified"],
        "statuses": statuses,
        "unexpected_keys": unexpected_keys,
        "unkeyed_records": unkeyed_records,
    }


def _final_result(ledger):
    first = ledger["first"]
    attempts = ledger["retries"]
    statuses = first["statuses"].copy()
    accepted = first["briefs"].copy()
    usage = Counter(first["usage"])
    unexpected = Counter(first["unexpected_keys"])
    unkeyed = first["unkeyed_records"]
    elapsed = first["elapsed_s"]
    for attempt in attempts.values():
        if attempt["state"] == "completed":
            statuses.update(attempt["statuses"])
            accepted.update(attempt["briefs"])
            usage.update(attempt["usage"])
            unexpected.update(attempt["unexpected_keys"])
            unkeyed += attempt["unkeyed_records"]
            elapsed += attempt["elapsed_s"]
    return {
        "input_sha256": ledger["input_sha256"],
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "batch_elapsed_s": round(elapsed, 3),
        "usage": dict(usage),
        "first_usage": first["usage"],
        "unknown_usage_attempts": (not first["usage"]) + sum(
            not a.get("usage") for a in attempts.values()
        ),
        "retry_attempts": {
            key: {name: value for name, value in attempt.items() if name != "briefs"}
            for key, attempt in attempts.items()
        },
        "coverage": {
            "first": _coverage(first["statuses"], first["unexpected_keys"], first["unkeyed_records"]),
            "final": _coverage(statuses, dict(unexpected), unkeyed),
        },
        "briefs": [accepted[key] for key in statuses if key in accepted],
    }


def run_batch(batch, invocation, output, *, prompt=PROMPT):
    if len({row["key"] for row in batch}) != len(batch):
        raise ValueError("Ключи задач во входном пакете должны быть уникальны")
    schema = (invocation / "schema.json").read_bytes()
    schema_sha = sha256(schema).hexdigest()
    fingerprint = sha256(json.dumps(
        [MODEL, EFFORT, COVERAGE_VERSION, prompt, schema_sha, batch], ensure_ascii=False,
    ).encode()).hexdigest()

    checkpoint_dir = output.parent / ".batch-ledgers"
    checkpoint_dir.mkdir(exist_ok=True)
    checkpoint = checkpoint_dir / output.name
    # SQLite releases the writer lock if the worker exits mid-attempt.
    with closing(sqlite3.connect(checkpoint.with_suffix(".lock.sqlite3"), timeout=0)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        if output.exists():
            _load_matching(output, fingerprint)
            return
        ledger = _load_matching(checkpoint, fingerprint) if checkpoint.exists() else None
        schema_path = invocation / f".schema-{schema_sha}.json"
        _write_bytes(schema_path, schema)
        if ledger is None:
            first = _invoke_batch(batch, invocation, output, prompt, schema_path)
            ledger = {"input_sha256": fingerprint, "first": first, "retries": {}}
            _write_json(checkpoint, ledger)
        retries = ledger["retries"]
        for index, row in enumerate(batch, 1):
            key = row["key"]
            if ledger["first"]["statuses"][key] != "missing":
                continue
            if key in retries:
                if retries[key]["state"] == "started":
                    retries[key]["state"] = "interrupted"
                    _write_json(checkpoint, ledger)
                continue
            retries[key] = {"state": "started"}
            _write_json(checkpoint, ledger)
            retry_output = checkpoint_dir / f"{output.stem}-retry-{index}.json"
            try:
                retries[key] = _invoke_batch([row], invocation, retry_output, prompt, schema_path)
            except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError, IndexError) as error:
                retries[key] = {"state": "failed", "error": type(error).__name__}
            _write_json(checkpoint, ledger)
        result = _final_result(ledger)
        _write_json(output, result)
        print(output.name, len(batch), result["usage"], flush=True)


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
    _write_json(invocation / "schema.json", SCHEMA)
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
                    if all(p.exists() for p in inputs):
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
                except Exception as error:  # noqa: BLE001 -- Collect failures from all workers.
                    errors[index] = str(error)
            if errors or pending:
                raise RuntimeError(f"Failed batches {errors}; waiting for retrieval {list(pending)}")


if __name__ == "__main__":
    main()
