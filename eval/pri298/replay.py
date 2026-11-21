"""Replay Git lexical retrieval against the frozen PRI-278 hybrid contexts.

Run as python -m eval.pri298.replay from the implementation checkout. Prepare
writes inputs for PRI-278's briefs.py. Summarize audits its three paired runs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
import statistics
import subprocess
from time import perf_counter

import yaml

from reviewer.gitutil import file_at_ref
from reviewer.index.store import Retrieved
from reviewer.mcp.brief_evidence import _visible_lines
from reviewer.policy.context_limits import CodeSectionLimits
from reviewer.retrieval.retriever import ContextPack

GROUPS = {
    "original": ("corpus.json", "evidence.jsonl.gz", "run", ("tune-30", "holdout-30")),
    "independent": (
        "independent-corpus.json", "independent/evidence.jsonl.gz", "variant", ("all-30",),
    ),
}
ARMS = ("off", "wide", "narrow")
# Pin the PRI-278 settings even if later production defaults change.
LIMITS = CodeSectionLimits(max_files=20, max_chunks_per_file=1, chars_per_file=975)
GIT_LIMIT = 3
HEADER = re.compile(r"^// (.+) \((.+):(\d+)-(\d+)\)$")
NUMBERED = re.compile(r"^\s*(\d+) \| (.*)$")
# Follow the frozen PRI-278 Luna prompt's citation format.
REFERENCE = re.compile(r"^- `?([^\s`]+):(\d+)`?[^\S\n]+[—–-][^\S\n]+(\S[^\n]*)", re.M)
REJECTED = re.compile(r"^(?:отброшено|исключено|dropped|excluded)\s*:", re.I)
PILOT = {
    "original": {"wide": 0, "narrow": 7, "total": 24},
    "independent": {"wide": 0, "narrow": 3, "total": 14},
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value, *, compact_pairs=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".tmp").open("w", encoding="utf-8") as file:
        if compact_pairs:
            pairs = value["pairs"]
            aggregate = {key: data for key, data in value.items() if key != "pairs"}
            top = json.dumps(aggregate, ensure_ascii=False, indent=2).removesuffix("\n}")
            rows = ",\n".join(
                "    " + json.dumps(pair, ensure_ascii=False, separators=(",", ":"))
                for pair in pairs
            )
            file.write(f'{top},\n  "pairs": [\n{rows}\n  ]\n}}')
        else:
            json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    path.with_suffix(".tmp").replace(path)


def frozen_inputs(pri278):
    cases, archived = [], {}
    for group, (corpus, evidence, selector, runs) in GROUPS.items():
        members = read(pri278 / corpus)
        selected = {}
        with gzip.open(pri278 / evidence, "rt", encoding="utf-8") as file:
            for line in file:
                row = json.loads(line)
                if row.get(selector) in runs and "retrieval" in row:
                    retrieval = row["retrieval"]
                    key = retrieval["key"]
                    if key in selected:
                        raise ValueError(f"Repeated archived retrieval {group}/{key}")
                    selected[key] = retrieval
        if (len(members) != len(selected)
                or {case["key"] for case in members} != set(selected)
                or set(selected) & archived.keys()):
            raise ValueError(f"Archived cases do not pair uniquely in {group}")
        for case in members:
            retrieval = selected[case["key"]]
            if retrieval["base_sha"] != case["base_sha"]:
                raise ValueError(f"Archived SHA mismatch for {case['key']}")
            cases.append({**case, "corpus": group})
            archived[case["key"]] = retrieval["modes"]["hybrid"]
    return cases, archived


def frozen_chunks(context):
    """Recover the exact visible excerpts so the production allocator can use them."""
    items = []
    for block in context.split("\n\n") if context else ():
        header, *rows = block.split("\n")
        match = HEADER.fullmatch(header)
        if not match or not rows:
            raise ValueError(f"Unexpected frozen excerpt: {header!r}")
        node, path, first, last = match.groups()
        start, end = int(first), int(last)
        body = []
        for number, row in enumerate(rows, start):
            match = NUMBERED.fullmatch(row)
            if match is None or int(match[1]) != number:
                raise ValueError(f"Invalid frozen excerpt numbering for {path}:{number}")
            body.append(match[2])
        items.append(Retrieved(node, path, node, "frozen", start, end, "\n".join(body), 0.0))
    if ContextPack(items=items).as_context(line_numbers=True) != context:
        raise ValueError("Frozen context failed exact render round trip")
    return items


def context_digest(context):
    return sha256(context.encode("utf-8")).hexdigest()


def arm_mode(context, selected, archived, ranked, git_selected, scan_ms):
    if len(selected) > LIMITS.max_files or len({item.path for item in selected}) != len(selected):
        raise ValueError("Replay exceeded its file or chunk limit")
    if any(len(item.text) > LIMITS.chars_per_file for item in selected):
        raise ValueError("Replay exceeded its per-file source budget")
    if len(ranked) > GIT_LIMIT or len(git_selected) > GIT_LIMIT:
        raise ValueError("Replay exceeded its Git quota")
    return {
        "context": context,
        "context_sha256": context_digest(context),
        "context_chars": len(context),
        "source_chars": sum(len(item.text) for item in selected),
        "selected_paths": [item.path for item in selected],
        "git_ranked_paths": [item.path for item in ranked],
        "git_selected_paths": [item.path for item in git_selected],
        "git_scan_ms": scan_ms,
        "archived_hybrid_ms": archived["elapsed_ms"],
        "render_truncated": "[...truncated]" in context.splitlines(),
    }


def read_batches(work, arm):
    return [read(path) for path in sorted((work / f"luna-all-{arm}").glob("batch-*.json"))]


def prepare(pri278, work, repo, arms):
    cases, archived = frozen_inputs(pri278)
    # Import here so copying the frozen control can run independently.
    enabled = tuple(arm for arm in ("wide", "narrow") if arm in arms)
    if enabled:
        from reviewer.retrieval.git_lexical import search_git
        from reviewer.retrieval.multiquery import reserve_git_excerpts

    completed = {
        arm: {row["key"] for batch in read_batches(work, arm) for row in batch["briefs"]}
        for arm in arms
    }
    off = ("off",) if "off" in arms else ()
    model_inputs = [{key: case[key] for key in ("key", "input", "split")} for case in cases]
    write(work / "corpus.json", model_inputs)
    for index, case in enumerate(cases):
        key, ref = case["key"], case["base_sha"]
        baseline = archived[key]
        original = frozen_chunks(baseline["context"])
        control = arm_mode(baseline["context"], original, baseline, [], [], 0.0)
        for field in ("source_chars", "context_chars", "selected_paths"):
            if control[field] != baseline[field]:
                raise ValueError(f"Frozen control {field} disagrees for {key}")

        if enabled:
            policy = yaml.safe_load(file_at_ref(str(repo), ".review.yml", ref) or "") or {}
            ignore = (policy.get("paths") or {}).get("ignore") or []
        # Alternate scan order to reduce systematic warm-filesystem effects.
        scan_order = enabled[::-1] if index % 2 else enabled
        for arm in (*off, *scan_order):
            if arm == "off":
                mode = control
            else:
                start = perf_counter()
                ranked = search_git(
                    repo, ref, case["input"], ignore=ignore,
                    limit=GIT_LIMIT, chars_per_file=LIMITS.chars_per_file, wide=arm == "wide",
                )
                scan_ms = round((perf_counter() - start) * 1000, 3)
                selected = reserve_git_excerpts(
                    original, ranked,
                    max_files=LIMITS.max_files, max_chunks_per_file=LIMITS.max_chunks_per_file,
                )
                git_selected = [item for item in selected if item in ranked]
                context = ContextPack(items=selected, max_chars=LIMITS.max_chars).as_context(
                    line_numbers=True
                )
                mode = arm_mode(context, selected, baseline, ranked, git_selected, scan_ms)
            label = f"all-{arm}"
            target = work / label / f"{key}.json"
            if key in completed[arm] and (
                not target.exists() or read(target)["modes"]["hybrid"]["context"] != mode["context"]
            ):
                raise ValueError(f"Prepared context missing or changed after model runs: {label}/{key}")
            write(target, {"key": key, "base_sha": ref, "modes": {"hybrid": mode}})


def gold_paths(repo, case):
    tree = subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "-rz", case["base_sha"]]
    )
    regular = set()
    for entry in tree.split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            if meta.split()[0] in (b"100644", b"100755"):
                regular.add(path.decode("utf-8", errors="surrogateescape"))
    core = {label["path"] for label in case["obligatory"]} & regular
    return {
        "non_python": {path for path in core if not path.endswith(".py")},
        "python": {path for path in core if path.endswith(".py")},
    }


def model_briefs(work, arm, keys):
    label = f"all-{arm}"
    briefs, usage, settings = {}, Counter(), set()
    batches = read_batches(work, arm)
    elapsed_s = []
    for batch in batches:
        settings.add((batch.get("model"), batch.get("reasoning_effort")))
        usage.update(batch.get("usage") or {})
        if "batch_elapsed_s" in batch:
            elapsed_s.append(batch["batch_elapsed_s"])
        for row in batch["briefs"]:
            if row["key"] in briefs:
                raise ValueError(f"Repeated brief {label}/{row['key']}")
            briefs[row["key"]] = row["brief"]
    if set(briefs) != keys:
        raise ValueError(
            f"Incomplete paired briefs for {label}. "
            f"Missing {sorted(keys - briefs.keys())}, extra {sorted(briefs.keys() - keys)}"
        )
    if len(settings) != 1:
        raise ValueError(f"Model settings differ across {label} batches")
    model, effort = settings.pop()
    return briefs, {
        "batches": len(batches), "model": model, "reasoning_effort": effort,
        "usage": dict(usage), "batch_elapsed_s": distributions(elapsed_s),
    }


def distributions(values):
    if not values:
        return None
    p95 = statistics.quantiles(values, n=100, method="inclusive")[94] if len(values) > 1 else (
        values[0]
    )
    return {
        "min": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(p95, 3),
        "max": round(max(values), 3),
        "sum": round(sum(values), 3),
    }


def audit_brief(text, visible):
    confirmed, invalid = [], []
    for path, line, reason in REFERENCE.findall(text):
        if REJECTED.match(reason):
            continue
        number = int(line)
        if number in visible.get(path, ()):
            confirmed.append({"path": path, "line": number})
        else:
            invalid.append({
                "path": path, "line": number,
                "reason": "outside_excerpt" if path in visible else "path_unseen",
            })
    return confirmed, invalid


def score_arm(mode, gold, brief):
    visible = _visible_lines(mode["context"])
    stages = {
        "ranked": set(mode["git_ranked_paths"]),
        "rendered": set(visible),
    }
    citations = {}
    if brief is not None:
        confirmed, invalid = audit_brief(brief, visible)
        stages["brief"] = {ref["path"] for ref in confirmed}
        citations = {"confirmed_citations": confirmed, "invalid_references": invalid}
    hits = {
        stage: {kind: sorted(paths & expected) for kind, expected in gold.items()}
        for stage, paths in stages.items()
    }
    return {
        "hits": hits,
        "rendered_files": len(visible),
        **{name: mode[name] for name in (
            "context_sha256", "source_chars", "context_chars", "selected_paths",
            "git_ranked_paths", "git_selected_paths", "git_scan_ms",
            "archived_hybrid_ms", "render_truncated",
        )},
        **citations,
    }


def aggregate(pairs, arm):
    rows = [pair["arms"][arm] for pair in pairs]
    coverage = {}
    for stage in rows[0]["hits"]:
        coverage[stage] = {}
        for kind in pairs[0]["gold"]:
            matches = [len(row["hits"][stage][kind]) for row in rows]
            totals = [len(pair["gold"][kind]) for pair in pairs]
            rates = [found / total for found, total in zip(matches, totals, strict=True) if total]
            coverage[stage][kind] = {
                "found": sum(matches), "total": sum(totals),
                "macro_mean": round(statistics.mean(rates), 5) if rates else None,
                "macro_median": round(statistics.median(rates), 5) if rates else None,
            }
    return {
        "coverage": coverage,
        **{field: distributions([row[field] for row in rows]) for field in (
            "rendered_files", "source_chars", "context_chars", "git_scan_ms", "archived_hybrid_ms",
        )},
        "render_truncations": sum(row["render_truncated"] for row in rows),
        "confirmed_citations": sum(len(row.get("confirmed_citations", [])) for row in rows),
        "invalid_references": sum(len(row.get("invalid_references", [])) for row in rows),
    }


def summarize(pri278, work, repo, retrieval_only):
    cases, archived = frozen_inputs(pri278)
    keys = {case["key"] for case in cases}
    models = {arm: model_briefs(work, arm, keys) for arm in ARMS} if not retrieval_only else {}
    if models and len({
        (run["model"], run["reasoning_effort"]) for _, run in models.values()
    }) != 1:
        raise ValueError("Model settings differ between arms")
    pairs = []
    for case in cases:
        key, group = case["key"], case["corpus"]
        gold = gold_paths(repo, case)
        arms = {}
        for arm in ARMS:
            prepared = read(work / f"all-{arm}" / f"{key}.json")
            mode = prepared["modes"]["hybrid"]
            if (prepared["base_sha"] != case["base_sha"]
                    or mode["context_sha256"] != context_digest(mode["context"])):
                raise ValueError(f"Prepared context does not match recorded metadata: {arm}/{key}")
            if arm == "off" and mode["context"] != archived[key]["context"]:
                raise ValueError(f"Changed control for {key}")
            brief = models[arm][0][key] if models else None
            arms[arm] = score_arm(mode, gold, brief)
        pairs.append({
            "key": key, "corpus": group, "base_sha": case["base_sha"],
            "gold": {kind: sorted(paths) for kind, paths in gold.items()},
            "arms": arms,
        })

    corpora = {}
    for group in (*GROUPS, "combined"):
        members = [pair for pair in pairs if group == "combined" or pair["corpus"] == group]
        corpora[group] = {
            "tasks": len(members),
            "arms": {arm: aggregate(members, arm) for arm in ARMS},
        }
    pilot = {}
    for group, expected in PILOT.items():
        ranked = {
            arm: corpora[group]["arms"][arm]["coverage"]["ranked"]["non_python"]
            for arm in ("wide", "narrow")
        }
        pilot[group] = {
            "expected": expected,
            "observed": {arm: count["found"] for arm, count in ranked.items()},
            "matches": all(
                count["found"] == expected[arm] and count["total"] == expected["total"]
                for arm, count in ranked.items()
            ),
        }
    return {
        "settings": {
            "max_files": LIMITS.max_files,
            "max_chunks_per_file": LIMITS.max_chunks_per_file,
            "chars_per_file": LIMITS.chars_per_file,
            "source_chars_budget": LIMITS.max_files * LIMITS.chars_per_file,
            "render_chars_limit": LIMITS.max_chars,
            "max_git_files": GIT_LIMIT,
            "git_scan_order": "alternating wide/narrow and narrow/wide in corpus order",
        },
        "pilot_reproduction": pilot,
        "corpora": corpora,
        "model_runs": {arm: run for arm, (_, run) in models.items()},
        "pairs": pairs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Prepare frozen and Git-augmented contexts")
    summary = commands.add_parser("summarize", help="Score the contexts and paired model outputs")
    for command in (prep, summary):
        command.add_argument("--pri278", type=Path, required=True, help="Historical artifacts")
        command.add_argument("--work", type=Path, required=True, help="Local scratch directory")
        command.add_argument("--repo", type=Path, default=Path.cwd(), help="Git history checkout")
    prep.add_argument("--arms", nargs="+", choices=ARMS, default=ARMS)
    summary.add_argument("--output", type=Path, help="Defaults to WORK/results.json")
    summary.add_argument(
        "--retrieval-only", action="store_true", help="Summarize without the model outputs",
    )
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.pri278, args.work, args.repo, args.arms)
    else:
        result = summarize(args.pri278, args.work, args.repo, args.retrieval_only)
        write(args.output or args.work / "results.json", result, compact_pairs=True)


if __name__ == "__main__":
    main()
