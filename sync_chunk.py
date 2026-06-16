#!/usr/bin/env python3
"""Index a slice of YouGile tasks into the reviewer task graph + vector store.

Run from project root with the venv activated:
    .venv/bin/python sync_chunk.py --offset 0 --limit 10
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def build_brief(task: dict, url_template: str, status: str) -> dict | None:
    key = task.get("idTaskCommon")
    alias = task.get("idTaskProject")
    if not key or not alias:
        return None
    url = url_template.replace("{code}", str(alias))
    return {
        "key": str(key),
        "aliases": [str(alias)],
        "title": task.get("title") or "",
        "description": task.get("description") or "",
        "status": status,
        "url": url,
        "criteria": [],
        "links": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--tasks-json", default="tasks.json")
    parser.add_argument("--results-jsonl", default="sync_results.jsonl")
    parser.add_argument("--url-template",
                        default="https://ru.yougile.com/team/686c049c8af8/{code}")
    parser.add_argument("--status", default="Backlog")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--sleep-seconds", type=float, default=60.0)
    args = parser.parse_args()

    tasks_path = Path(args.tasks_json)
    results_path = Path(args.results_jsonl)

    if not tasks_path.exists():
        print(f"ERROR: tasks file not found: {tasks_path}", file=sys.stderr)
        return 1

    with tasks_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    all_tasks = data.get("content", [])
    total = len(all_tasks)
    end = min(args.offset + args.limit, total)
    slice_tasks = all_tasks[args.offset:end]

    if not slice_tasks:
        print(f"No tasks in offset={args.offset} limit={args.limit} (total={total})")
        return 0

    print(f"Chunk tasks {args.offset}-{end-1} of {total}")

    try:
        from reviewer.app import build_components
        from reviewer.config.settings import Settings
        from reviewer.mcp.service import MCPReviewService
    except Exception as e:
        print(f"ERROR: cannot import reviewer modules: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2

    try:
        settings = Settings()
        components = build_components(settings)
        service = MCPReviewService(settings, components)
    except Exception as e:
        print(f"ERROR: reviewer MCP components unreachable: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2

    skipped = 0
    failed = 0
    embedded = 0
    refreshed = 0

    results_path.parent.mkdir(parents=True, exist_ok=True)

    for batch_index in range(0, len(slice_tasks), args.batch_size):
        batch = slice_tasks[batch_index:batch_index + args.batch_size]
        for task in batch:
            brief = build_brief(task, args.url_template, args.status)
            record: dict = {
                "key": None,
                "alias": None,
                "embedded": False,
                "refreshed": False,
                "success": False,
                "skipped": False,
                "error": None,
                "warnings": [],
            }
            if brief is None:
                record["skipped"] = True
                record["error"] = "missing idTaskCommon or idTaskProject"
                skipped += 1
            else:
                record["key"] = brief["key"]
                record["alias"] = brief["aliases"][0]
                try:
                    result = service.index_task(brief)
                    record["success"] = True
                    record["embedded"] = bool(result.get("embedded"))
                    record["refreshed"] = not record["embedded"]
                    record["warnings"] = result.get("warnings") or []
                    if record["embedded"]:
                        embedded += 1
                    else:
                        refreshed += 1
                except Exception as e:
                    record["error"] = f"{type(e).__name__}: {e}"
                    failed += 1

            with results_path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()

            print(f"  {record['key'] or 'NO-KEY'}: "
                  f"embedded={record['embedded']} refreshed={record['refreshed']} "
                  f"success={record['success']} skipped={record['skipped']} "
                  f"warnings={len(record['warnings'])}")

        if batch_index + args.batch_size < len(slice_tasks):
            print(f"  sleeping {args.sleep_seconds}s before next batch...")
            time.sleep(args.sleep_seconds)

    print(f"Chunk done: embedded={embedded}, refreshed={refreshed}, "
          f"failed={failed}, skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
