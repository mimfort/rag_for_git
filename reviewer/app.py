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


def _close_resources(
    resources,
    *,
    primary_error: BaseException | None = None,
) -> None:
    """Закрыть ресурсы по порядку, не маскируя исходную construction-ошибку."""
    first_error: Exception | None = None
    seen: set[int] = set()
    for resource in resources:
        if resource is None or id(resource) in seen:
            continue
        seen.add(id(resource))
        try:
            resource.close()
        except Exception as error:  # noqa: BLE001 - cleanup обязан продолжиться
            if primary_error is not None:
                primary_error.add_note(f"Ошибка cleanup {type(resource).__name__}: {error!r}")
            else:
                first_error = first_error or error
    if first_error is not None:
        raise first_error


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
        _close_resources(
            (
                self.summary_store,
                self.sync_service,
                self.subtask_operation_store,
                self.task_store,
                self.graph,
                self.store,
            )
        )


def _voyage_client(settings: Settings):
    import voyageai
    # ключ берём из Settings (.env), а не из os.environ
    return voyageai.Client(api_key=settings.voyage_api_key or None)

def build_components(settings: Settings, connect: bool = True) -> Components:
    created: list[object] = []
    try:
        store = ChunkStore(
            settings.pg_dsn,
            min_size=settings.pg_pool_min_size,
            max_size=settings.pg_pool_max_size,
        )
        created.append(store)
        vclient = _voyage_client(settings)
        embedder = VoyageEmbedder(client=vclient, model=settings.embedding_model,
                                  dim=settings.embedding_dim,
                                  batch_size=settings.embedding_batch_size,
                                  token_budget=settings.embedding_token_budget)
        reranker = VoyageReranker(client=vclient, model=settings.rerank_model)
        graph = GraphStore(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password,
                           connection_timeout=settings.neo4j_connection_timeout,
                           acquisition_timeout=settings.neo4j_acquisition_timeout,
                           max_retry_time=settings.neo4j_max_retry_time) \
            if connect else None
        if graph is not None:
            created.append(graph)
        retriever = Retriever(store, graph, embedder, reranker,
                              max_context_chars=settings.max_tool_result_chars)
        task_store = TaskStore(
            settings.pg_dsn,
            min_size=settings.pg_pool_min_size,
            max_size=settings.pg_pool_max_size,
        )
        created.append(task_store)
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
        created.append(subtask_operation_store)
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
        created.extend(providers)
        sync_providers = [
            SyncProvider(
                provider,
                board_credentials.secret_values(board_registry.get(provider.board_type)),
                owned=True,
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
        created.append(summary_store)
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
    except BaseException as error:
        _close_resources(reversed(created), primary_error=error)
        raise
