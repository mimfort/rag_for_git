"""PRI-278 extension of the solve-task replay for pinned pre-fix snapshots."""
from __future__ import annotations

import argparse
import json
import pickle
import subprocess
import time
from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pgvector import Vector

from eval.solve_task_metrics.context_paths import extract_context_paths
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.graph.builder import build_graph_from_files
from reviewer.index.chunker import chunk_python
from reviewer.index.pathfilter import is_ignored
from reviewer.index.store import Retrieved, _bm25_query
from reviewer.policy.context_limits import ContextLimits
from reviewer.retrieval.multiquery import cap_block, diversify_by_file, rrf_merge
from reviewer.retrieval.retriever import ContextPack, _dedupe_overlapping, _is_test_path
from reviewer.rrf import RRF_K

CORPUS = Path(__file__).with_name("corpus.json")
REPO = "mimfort/rag_for_git"
PIPELINE_SHA = "2ae5721df4e4deb608b8b5fcf6fe6041167c8099"
LIMITS = ContextLimits()
K = 10
MODES = ("bm25", "ann", "hybrid", "hybrid_rerank")


def git(*args):
    return subprocess.check_output(["git", *args])


@contextmanager
def blob_reader():
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as process:
        def read(object_id):
            process.stdin.write(object_id.encode() + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            size = int(header[-1])
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b"\n":
                raise ValueError(f"Incomplete git blob {object_id}")
            return data

        yield read
        process.stdin.close()


def snapshot_sources(sha, read, blobs):
    files = {}
    for entry in git("ls-tree", "-rz", sha).split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            if meta.split()[1] == b"blob":
                files[path.decode()] = meta.split()[-1].decode()
    policy = yaml.safe_load(read(files[".review.yml"])) if ".review.yml" in files else {}
    ignored = ((policy or {}).get("paths") or {}).get("ignore") or []
    sources = {}
    for path, object_id in files.items():
        if path.endswith(".py") and not is_ignored(path, ignored):
            if object_id not in blobs:
                blobs[object_id] = read(object_id).decode("utf-8")
            sources[path] = blobs[object_id]
    return sources


def prepare_snapshots(cases):
    snapshots, chunks, texts, blobs = {}, {}, {}, {}
    with blob_reader() as read:
        for case in cases:
            sources = snapshot_sources(case["base_sha"], read, blobs)
            by_path = {}
            for path, source in sources.items():
                identity = (path, source)
                if identity not in chunks:
                    chunks[identity] = chunk_python(path, source.encode())
                    texts.update((c.content_hash, c.text) for c in chunks[identity])
                by_path[path] = chunks[identity]
            snapshots[case["key"]] = (sources, by_path)
    return snapshots, texts


def embeddings(components, texts, cache_path):
    vectors = {}
    if cache_path.exists():
        # Only read this local cache created by this experiment.
        with cache_path.open("rb") as file:
            vectors.update(pickle.load(file))
    missing = [key for key in texts if key not in vectors]
    vectors.update(components.store.find_embeddings_by_hashes(REPO, missing))
    missing = [key for key in missing if key not in vectors]
    for offset in range(0, len(missing), 200):
        batch = missing[offset:offset + 200]
        output = components.embedder.embed_documents([texts[key] for key in batch])
        vectors.update(zip(batch, output, strict=True))
        with cache_path.with_suffix(".tmp").open("wb") as file:
            pickle.dump(vectors, file)
        cache_path.with_suffix(".tmp").replace(cache_path)
    return vectors


def make_index(conn, chunks, vectors):
    # All DDL and data are confined to this connection's temporary snapshot.
    conn.execute("""
        CREATE TEMP TABLE pri278_snapshot (
            id bigint PRIMARY KEY, text text NOT NULL,
            embedding vector(1024) NOT NULL, is_test boolean NOT NULL
        ) ON COMMIT DROP
    """)
    with conn.cursor().copy("COPY pri278_snapshot FROM STDIN") as copy:
        for i, chunk in enumerate(chunks, 1):
            copy.write_row((i, chunk.text, Vector(vectors[chunk.content_hash]), _is_test_path(chunk.path)))
    conn.execute("""CREATE INDEX ON pri278_snapshot USING bm25 (id, text)
                    WITH (key_field='id')""")
    conn.execute("""CREATE INDEX ON pri278_snapshot USING hnsw (embedding vector_cosine_ops)
                    WITH (m=16, ef_construction=64)""")
    conn.execute("ANALYZE pri278_snapshot")


def search(conn, query, vector, pool, production_only):
    where = " AND NOT is_test" if production_only else ""
    start = time.perf_counter()
    lexical = conn.execute(
        f"""SELECT id, pdb.score(id) FROM pri278_snapshot WHERE text @@@ %s {where}
            ORDER BY pdb.score(id) DESC LIMIT %s""",
        (_bm25_query(query), pool),
    ).fetchall()
    bm25_ms = elapsed(start)
    start = time.perf_counter()
    semantic = conn.execute(
        f"""SELECT id, embedding <=> %(vector)s AS distance
            FROM pri278_snapshot WHERE TRUE {where}
            ORDER BY embedding <=> %(vector)s LIMIT %(pool)s""",
        {"vector": Vector(vector), "pool": pool},
    ).fetchall()
    ann_ms = elapsed(start)
    return lexical, semantic, bm25_ms, ann_ms


def elapsed(start):
    return round((time.perf_counter() - start) * 1000, 3)


def tied_ranks(rows):
    """SQL RANK() semantics for equal neighbouring scores."""
    previous = None
    for i, (index, score) in enumerate(rows, 1):
        if i == 1 or score != previous:
            rank = i
        yield index, rank, score
        previous = score


def ranked_paths(items):
    return list(dict.fromkeys(item.path for item in items))


def stage_rows(rows, items):
    return [[items[index].node_id, rank, score] for index, rank, score in tied_ranks(rows)]


def merged_ids(lexical, semantic, pool):
    scores = defaultdict(float)
    for source in (lexical, semantic):
        for index, rank, _ in tied_ranks(source):
            scores[index] += 1 / (RRF_K + rank)
    return sorted(scores, key=lambda i: (-scores[i], i))[:pool]


def graph_children(sources):
    nodes, edges = build_graph_from_files(sources)
    graph = defaultdict(set)
    for origin, kind, target in edges:
        graph[origin].add(target)
        graph[target].add(origin)
    return graph, len(nodes), len(edges)


def expand(ranked, graph, by_id):
    """One hop from the first fifteen chunks, matching the replay graph direction."""
    neighbors = {
        neighbor
        for item in ranked[:LIMITS.search_codebase.ceiling]
        for neighbor in graph.get(item.node_id, ())
    }
    already = {item.node_id for item in ranked}
    return [by_id[node] for node in sorted(neighbors - already) if node in by_id]


def render(ranked, graph, by_id, *, expand_neighbors=expand):
    section = LIMITS.code_section
    expanded = [*ranked, *expand_neighbors(ranked, graph, by_id)]
    production = [item for item in expanded if not _is_test_path(item.path)]
    selected = diversify_by_file(
        _dedupe_overlapping(production),
        max_files=section.max_files,
        max_chunks_per_file=section.max_chunks_per_file,
    )
    pack = ContextPack(
        items=[cap_block(item, section.chars_per_file) for item in selected],
        max_chars=section.max_chars,
    )
    context = pack.as_context(line_numbers=True)
    visible = extract_context_paths(context)
    return context, {
        "ranked_paths": ranked_paths(item for item in ranked if not _is_test_path(item.path)),
        "expanded_paths": ranked_paths(production),
        "selected_paths": ranked_paths(selected),
        "context_paths": [path for path in ranked_paths(selected) if path in visible],
        "context_chars": len(context),
        "source_chars": sum(len(item.text) for item in pack.items),
    }


def make_item(chunk):
    return Retrieved(
        chunk.node_id, chunk.path, chunk.symbol_fqn, chunk.kind,
        chunk.start_line, chunk.end_line, chunk.text, 0.0,
    )


@dataclass
class PreparedCase:
    items: dict
    by_id: dict
    graph: dict
    graph_nodes: int
    graph_edges: int
    graph_ms: float
    index_ms: float


def prepare_case(snapshot, vectors, conn):
    sources, index = snapshot
    chunks = [chunk for file_chunks in index.values() for chunk in file_chunks]
    items = {i: make_item(c) for i, c in enumerate(chunks, 1)}
    by_id = {item.node_id: item for item in items.values()}
    start = time.perf_counter()
    make_index(conn, chunks, vectors)
    index_ms = elapsed(start)
    start = time.perf_counter()
    graph, nodes, edges = graph_children(sources)
    return PreparedCase(items, by_id, graph, nodes, edges, elapsed(start), index_ms)


def evaluate(case, prepared, components, conn, queries_vectors, candidate, modes, *,
             merge_limit=None):
    items = prepared.items
    pool, production_only = candidate
    merge_limit = pool if merge_limit is None else merge_limit
    trace, per_mode = [], defaultdict(list)
    latency = defaultdict(float)
    for query, vector, embed_ms in queries_vectors:
        lexical, semantic, bm25_ms, ann_ms = search(conn, query, vector, pool, production_only)
        start = time.perf_counter()
        ids = merged_ids(lexical, semantic, merge_limit)
        distances = dict(semantic)
        lexical_ids = {i for i, _ in lexical}
        distance = LIMITS.search_codebase.ann_distance_max
        qualified = [i for i in ids if i in lexical_ids or distances[i] <= distance]
        rrf_ms = elapsed(start)
        per_mode["bm25"].append([items[i] for i, _ in lexical])
        per_mode["ann"].append([items[i] for i, d in semantic if d <= distance])
        per_mode["hybrid"].append([items[i] for i in qualified])
        trace.append({
            "query": query,
            "bm25_ms": bm25_ms, "ann_ms": ann_ms, "embed_ms": embed_ms,
            "bm25": stage_rows(lexical, items),
            "ann": stage_rows(semantic, items),
            "fused": [items[i].node_id for i in ids],
            "qualified": [items[i].node_id for i in qualified],
        })
        latency["bm25"] += bm25_ms
        latency["ann"] += embed_ms + ann_ms
        latency["hybrid"] += embed_ms + ann_ms + bm25_ms + rrf_ms
    payload = {}
    failure = {}
    for mode in modes:
        start = time.perf_counter()
        ranked = rrf_merge(per_mode["hybrid" if mode == "hybrid_rerank" else mode])
        scores = []
        try:
            if mode == "hybrid_rerank":
                scored = components.reranker.rerank_scored(queries_vectors[0][0], ranked)
                if {item.node_id for item, _ in scored} != {item.node_id for item in ranked}:
                    raise ValueError("Incomplete reranking")
                ranked = [replace(item, score=score) for item, score in scored]
                scores = [[item.node_id, i, score] for i, (item, score) in enumerate(scored, 1)]
        except Exception as error:
            failure[mode] = type(error).__name__
            continue
        context, stage = render(ranked, prepared.graph, prepared.by_id)
        payload[mode] = dict(
            stage, context=context, rerank=scores,
            elapsed_ms=round(latency["hybrid" if mode == "hybrid_rerank" else mode] + elapsed(start), 3),
        )
    return {
        "key": case["key"],
        "base_sha": case["base_sha"], "candidate_pool": pool, "production_only": production_only,
        "merge_limit": merge_limit,
        "chunks": len(items), "index_paths": sorted({item.path for item in items.values()}),
        "graph_nodes": prepared.graph_nodes, "graph_edges": prepared.graph_edges,
        "graph_ms": prepared.graph_ms, "index_ms": prepared.index_ms,
        "models": {"embed": components.embedder.model, "rerank": components.reranker.model},
        "source_status": {"git": "ok", "paradedb": "ok", "voyage_embed": "ok",
                          "graph": "treesitter", **failure},
        "traces": trace, "modes": payload, "failures": failure,
    }


def save_row(directory, row):
    path = directory / f"{row['key']}.json"
    path.with_suffix(".tmp").write_text(json.dumps(row, ensure_ascii=False) + "\n")
    path.with_suffix(".tmp").replace(path)
    print(row["key"], row["candidate_pool"], row["chunks"], row["failures"],
          {mode: len(data["context_paths"]) for mode, data in row["modes"].items()}, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="Local working and cache directory")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--split", choices=("all", "tune", "holdout"), default="all")
    parser.add_argument("--pool", type=int)
    parser.add_argument("--production-only", action="store_true")
    parser.add_argument("--hybrid-only", action="store_true")
    parser.add_argument("--paired-pools", action="store_true",
                        help="Compare 30 and 60 input candidates on the same snapshot")
    args = parser.parse_args()
    if args.paired_pools and (args.pool is not None or args.production_only):
        parser.error("--paired-pools fixes both pool sizes and includes test chunks in both arms")
    if args.pool is not None and args.pool < 1:
        parser.error("--pool must be positive")
    cases = [case for case in json.loads(args.corpus.read_text())
             if args.split in ("all", case["split"])]
    pool = LIMITS.search_codebase.candidate_pool if args.pool is None else args.pool
    pools = (30, 60) if args.paired_pools else (pool,)
    label = f"{args.split}-pool" if args.paired_pools else args.split
    suffix = "-prod" if args.production_only else ""
    outputs = {n: args.work / f"{label}-{n}{suffix}" for n in pools}
    for directory in outputs.values():
        directory.mkdir(parents=True, exist_ok=True)
    pending = [(i, case) for i, case in enumerate(cases)
               if any(not (d / f"{case['key']}.json").exists() for d in outputs.values())]
    if not pending:
        return
    snapshots, texts = prepare_snapshots([case for _, case in pending])
    with closing(build_components(Settings())) as components:
        vectors = embeddings(components, texts, args.work / "vectors.pkl")
        for i, case in pending:
            key = case["key"]
            try:
                start = time.perf_counter()
                query = case["input"]
                vector = components.embedder.embed_query(query)
                query_data = [(query, vector, elapsed(start))]
            except Exception as error:
                print(key, "voyage_embed", type(error).__name__, flush=True)
                continue
            try:
                with components.store._connect() as conn:
                    prepared = prepare_case(snapshots[key], vectors, conn)
                    if args.paired_pools:
                        conn.execute("SET LOCAL hnsw.ef_search = 60")
                    order = pools if i % 2 == 0 else pools[::-1]
                    rows = [evaluate(
                        case, prepared, components, conn, query_data, (n, args.production_only),
                        ("hybrid",) if args.hybrid_only or args.paired_pools else MODES,
                        merge_limit=pools[0] if args.paired_pools else None,
                    ) for n in order]
                    conn.commit()
            except Exception as error:
                print(key, "snapshot_or_search", type(error).__name__, flush=True)
                raise
            for j, row in enumerate(rows):
                if args.paired_pools:
                    row.update(ann_ef_search=60, pair_order=j + 1)
                save_row(outputs[row["candidate_pool"]], row)


if __name__ == "__main__":
    main()
