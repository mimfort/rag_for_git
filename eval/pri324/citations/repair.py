"""Offline rescore of narrow out-of-excerpt citation retries."""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from pathlib import Path

from eval.pri299 import pilot
from eval.pri310 import replay as pri310
from eval.pri324.citations.replay import HERE, inputs, parse_answer, source, verified_rows
from reviewer.mcp.brief_evidence import _visible_blocks

ARCHIVE = HERE / "repairs.json.gz"


def archived_briefs() -> dict:
    _, main_records = pri310.read_responses(HERE.parent.parent / "pri310", 4)
    main = {
        name: pilot.index_response(record["response"])
        for name, record in main_records.items() if name.endswith("-rank")
    }
    independent = {
        f"r{record['rep']}-structured": pilot.index_response(record["response"])
        for record in pilot.read_json(pilot.HERE / "responses.json")["runs"]
        if record["condition"] == "structured"
    }
    return main | independent


def submitted_references(brief: dict) -> list[tuple[str, int, str]]:
    if "references" in brief:
        return [
            (item["path"], item["line"], item["reason"])
            for item in brief["references"] if item["section"] == "code"
        ]
    return parse_answer(brief, None)[0]


def score(bundle: dict) -> dict:
    groups, labels = inputs()
    cases = {row["key"]: row for batches in groups.values() for batch in batches for row in batch}
    original = archived_briefs()
    result = {}
    for arm, records in bundle["arms"].items():
        result[arm] = {}
        for group, requests in records["requests"].items():
            request_payload = [request["input"] for request in requests]
            checksum = pilot.digest(json.dumps(request_payload, ensure_ascii=False))
            by_id = {}
            for request in requests:
                case = cases[request["key"]]
                if case["base_sha"] != request["base_sha"]:
                    raise ValueError(f"Изменился Git-снимок {request['key']}")
                path = request["input"]["path"]
                shown = {}
                for row in request["input"]["rows"]:
                    number, _, text = row.partition(" | ")
                    shown[int(number)] = text
                git_lines = (source(case["base_sha"], path) or "").split("\n")
                verified = verified_rows(case["base_sha"], case["context"])
                path_allowed = verified.get(path, set())
                submitted = submitted_references(original[request["source_run"]][request["key"]])
                anchor = request["input"]["original_line"]
                target = (path, anchor, request["input"]["reason"])
                if (
                    not path_allowed
                    or target not in submitted
                    or any(p == path and n in path_allowed for p, n, _ in submitted)
                ):
                    raise ValueError(f"Нет отклонённой ссылки на указанную строку {path}")
                if not shown or any(not 1 <= number <= len(git_lines) for number in shown):
                    raise ValueError(f"Строки выходят за пределы Git-объекта {path}")
                if arm == "visible":
                    render = {
                        n: text for rel_path, _, rows in _visible_blocks(case["context"])
                        if rel_path == path for n, text in rows if n in verified[path]
                    }
                    if shown != render:
                        raise ValueError(f"Исходный отрывок изменился {path}")
                elif (
                    anchor not in shown
                    or list(shown) != list(range(min(shown), max(shown) + 1))
                    or sum(len(line) + 1 for line in shown.values()) > 975
                    or any(git_lines[n - 1] != text for n, text in shown.items())
                ):
                    raise ValueError(f"Git-отрывок изменился {path}")
                ref_id = request["input"]["id"]
                if ref_id in by_id:
                    raise ValueError(f"Повторный идентификатор в запросе {ref_id}")
                by_id[ref_id] = request, shown

            for rep in (1, 2):
                name = f"{group}-{rep}"
                record = records["responses"][name]
                if record["input_sha256"] != checksum:
                    raise ValueError(f"Архив не соответствует запросу {arm} / {name}")
                applied = set()
                seen = set()
                accepted = []
                counts = Counter()
                for verdict in json.loads(record["response"])["checks"]:
                    ref_id = verdict["id"]
                    if ref_id not in by_id or ref_id in seen:
                        raise ValueError(f"Неизвестный или повторный идентификатор {ref_id}")
                    seen.add(ref_id)
                    request, shown = by_id[ref_id]
                    if verdict["line"] is None:
                        if verdict["text"] is not None:
                            raise ValueError(f"Строка без номера {ref_id}")
                        counts["abstained"] += 1
                        continue
                    if verdict["text"] is None or shown.get(verdict["line"]) != verdict["text"]:
                        counts["invalid_text"] += 1
                        continue
                    key = request["key"]
                    path = request["input"]["path"]
                    added = (key, request["source_run"], path)
                    if added not in applied:
                        applied.add(added)
                        gold = labels[key]
                        group_name = (
                            "large" if len(gold["core"]) >= pri310.MIN_LARGE_FILES else "ordinary"
                        )
                        category = next(
                            (name for name, paths in gold.items() if path in paths), "unlabelled"
                        )
                        counts[f"{group_name}_{category}"] += 1
                    accepted.append({
                        "key": key, "source_run": request["source_run"], "base_sha": request["base_sha"],
                        "path": path, "reason": request["input"]["reason"], "line": verdict["line"],
                        "text": verdict["text"],
                    })
                if seen != by_id.keys():
                    raise ValueError(f"Ответ {name} содержит пропуски")
                result[arm][name] = {
                    "counts": dict(counts), "accepted": accepted, "usage": record["usage"],
                }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = score(json.loads(gzip.decompress(ARCHIVE.read_bytes())))
    if args.output:
        pri310.write_json(args.output, result)
    print(json.dumps({
        arm: {record: row["counts"] for record, row in records.items()}
        for arm, records in result.items()
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
