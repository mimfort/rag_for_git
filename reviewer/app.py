from __future__ import annotations
from dataclasses import dataclass

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.index.embeddings import VoyageEmbedder
from reviewer.index.reranker import VoyageReranker
from reviewer.index.summary_store import SummaryStore
from reviewer.graph.store import GraphStore
from reviewer.retrieval.retriever import Retriever
from reviewer.tasks.store import TaskStore
from reviewer.tasks.graph import TaskGraph
from reviewer.tasks.service import TaskService
from reviewer.tasks.boards import make_board_providers
from reviewer.tasks.sync import SyncService

@dataclass
class Components:
    settings: Settings
    store: ChunkStore
    graph: GraphStore | None
    embedder: VoyageEmbedder
    reranker: VoyageReranker
    retriever: Retriever
    task_store: TaskStore
    task_graph: TaskGraph | None
    task_service: TaskService
    sync_service: SyncService | None
    summary_store: SummaryStore

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
    task_store = TaskStore(
        settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )
    task_graph = TaskGraph(graph.driver) if graph is not None else None
    task_service = TaskService(
        task_store, task_graph, embedder,
        max_chars=settings.max_tool_result_chars,
    )
    # server-side синк доски: провайдеры по настроенным типам. None, если
    # ни одна доска не настроена — sync_board вернёт понятный error-summary.
    _providers = make_board_providers(settings)
    provider = _providers[0] if _providers else None
    sync_service = SyncService(provider, task_service, store) \
        if provider is not None else None
    summary_store = SummaryStore(
        settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )
    return Components(settings, store, graph, embedder, reranker, retriever,
                      task_store, task_graph, task_service, sync_service,
                      summary_store)
