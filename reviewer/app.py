from __future__ import annotations

from dataclasses import dataclass

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.graph.store import GraphStore
from reviewer.index.embeddings import VoyageEmbedder
from reviewer.index.reranker import VoyageReranker
from reviewer.index.store import ChunkStore
from reviewer.index.summary_store import SummaryStore
from reviewer.retrieval.retriever import Retriever
from reviewer.tasks.boards import make_board_providers
from reviewer.tasks.boards.registry import default_board_registry
from reviewer.tasks.graph import TaskGraph
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import TaskStore
from reviewer.tasks.subtask_store import SubtaskOperationStore
from reviewer.tasks.subtasks import SubtaskService
from reviewer.tasks.sync import SyncProvider, SyncService


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
    subtask_operation_store: SubtaskOperationStore
    subtask_service: SubtaskService
    sync_service: SyncService | None
    summary_store: SummaryStore

    def close(self) -> None:
        """Закрыть долгоживущие store/graph ресурсы приложения."""
        first_error: Exception | None = None
        resources = (
            self.store,
            self.graph,
            self.task_store,
            self.subtask_operation_store,
            self.summary_store,
        )
        for resource in resources:
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as error:  # noqa: BLE001 - shutdown должен закрыть остальные stores
                first_error = first_error or error
        if first_error is not None:
            raise first_error


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
        attachment_embed_chars=settings.task_attachment_embed_chars,
    )
    subtask_operation_store = SubtaskOperationStore(
        settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )
    subtask_service = SubtaskService(subtask_operation_store)
    # server-side синк досок: все настроенные провайдеры (связка ключей в env).
    # Пустой список → sync_service=None, sync_board вернёт понятный error-summary.
    board_registry = default_board_registry()
    board_credentials = ProviderCredentialSource.from_settings(settings)
    providers = make_board_providers(
        settings,
        registry=board_registry,
        credential_source=board_credentials,
    )
    sync_providers = [
        SyncProvider(
            provider,
            board_credentials.secret_values(board_registry.get(provider.board_type)),
        )
        for provider in providers
    ]
    sync_service = (
        SyncService(sync_providers, task_service, store)
        if sync_providers
        else None
    )
    summary_store = SummaryStore(
        settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )
    return Components(
        settings=settings,
        store=store,
        graph=graph,
        embedder=embedder,
        reranker=reranker,
        retriever=retriever,
        task_store=task_store,
        task_graph=task_graph,
        task_service=task_service,
        subtask_operation_store=subtask_operation_store,
        subtask_service=subtask_service,
        sync_service=sync_service,
        summary_store=summary_store,
    )
