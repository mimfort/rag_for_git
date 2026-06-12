from __future__ import annotations
from dataclasses import dataclass

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.index.embeddings import VoyageEmbedder
from reviewer.index.reranker import VoyageReranker
from reviewer.graph.store import GraphStore
from reviewer.retrieval.retriever import Retriever

@dataclass
class Components:
    settings: Settings
    store: ChunkStore
    graph: GraphStore | None
    embedder: VoyageEmbedder
    reranker: VoyageReranker
    retriever: Retriever

def _voyage_client(settings: Settings):
    import voyageai
    # ключ берём из Settings (.env), а не из os.environ
    return voyageai.Client(api_key=settings.voyage_api_key or None)

def build_components(settings: Settings, connect: bool = True) -> Components:
    store = ChunkStore(
        settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )
    vclient = _voyage_client(settings)
    embedder = VoyageEmbedder(client=vclient, model=settings.embedding_model,
                              dim=settings.embedding_dim,
                              batch_size=settings.embedding_batch_size)
    reranker = VoyageReranker(client=vclient, model=settings.rerank_model)
    graph = GraphStore(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password) \
        if connect else None
    retriever = Retriever(store, graph, embedder, reranker,
                          max_context_chars=settings.max_tool_result_chars)
    return Components(settings, store, graph, embedder, reranker, retriever)
