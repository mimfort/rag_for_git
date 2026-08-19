"""Живой провайдер ретрива поверх компонентов reviewer (PRI-254).

ЕДИНСТВЕННЫЙ модуль харнесса с живыми зависимостями: Postgres, Neo4j, Voyage.
Остальные модули (в том числе replay.py) о нём не знают и тестируются на
фейках — источник ретрива приходит инъекцией, как run_git в build_snapshot.

Вызываются продакшн-методы MCPReviewService — те же, что дёргает
_TaskContextDeps при сборке контекста задачи. Своей копии пути ретрива здесь
не заводится, иначе replay мерил бы не то, что работает в проде.
"""
from __future__ import annotations

from dataclasses import asdict

from reviewer.app import build_components
from reviewer.config.branches import resolve_repo_branches
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.mcp.task_context import _query as production_query
from reviewer.policy.context_limits import ContextLimits
from reviewer.services.status import build_status_report


def limits_to_yaml(limits: ContextLimits) -> dict:
    """Сериализовать ContextLimits в блок context_limits формата .review.yml.

    Нужна, чтобы оверрайд одного ключа не обнулял остальные: from_review_yaml
    добирает недостающие ключи из КЛАССОВЫХ дефолтов, а не из лимитов репо.
    """
    return {
        "search_codebase": asdict(limits.search_codebase),
        "search_tasks": asdict(limits.search_tasks),
        "graph": asdict(limits.graph),
        "code_section": asdict(limits.code_section),
    }


def _merge(base: dict, overrides: dict | None) -> dict:
    """Наложить оверрайды на блок лимитов посекционно."""
    if not overrides:
        return base
    merged = {section: dict(values) for section, values in base.items()}
    for section, values in overrides.items():
        merged.setdefault(section, {}).update(values)
    return merged


class LiveRetrieval:
    """Провайдер секций replay поверх живого MCPReviewService."""

    def __init__(self, settings: Settings, components, service: MCPReviewService):
        self._settings = settings
        self._components = components
        self._service = service

    # -- жизненный цикл ---------------------------------------------------

    def __enter__(self) -> "LiveRetrieval":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть пул Postgres и драйвер Neo4j."""
        self._components.close()

    # -- секции -----------------------------------------------------------

    def preflight(self, repo: str, branch: str) -> dict:
        """Состояние base-индекса ветки: часть идентичности снимка."""
        report = build_status_report(
            self._components.store,
            self._components.graph,
            repo,
            [branch],
            self._service._repo_clone_path(repo) or "",
            summary_store=getattr(self._components, "summary_store", None),
        )
        status = report.branches[0]
        return {
            "branch": status.branch,
            "indexed_sha": status.indexed_sha,
            "drift": status.drift,
            "summaries": status.summaries,
            "chunks": status.chunks,
            "graph_nodes": status.graph_nodes,
        }

    def task(self, key: str) -> dict | None:
        """Нормализованная задача из стора reviewer (не из брифа)."""
        return self._service.get_task(key)

    def query(self, task: dict | None, key: str) -> str:
        """Запрос ретрива продакшн-формулой, без своей копии.

        Копия формулы запроса — тот же класс дефекта, что PRI-249 запрещает
        для формул метрики: replay мерил бы не тот вход, что видит прод.
        """
        return production_query(task, key)

    def code(self, repo: str, branch: str, query: str, limits: dict | None) -> str:
        """Выдача ретрива по коду в том же виде, в каком её получает сборщик брифа.

        Без оверрайдов зовётся продакшн-метод search_codebase дословно. С
        оверрайдами приходится идти на уровень ниже (search_codebase не
        принимает лимиты параметром) — рендер при этом тот же as_context.
        """
        if not limits:
            return self._service.search_codebase(repo, query, None, branch, False)
        base = limits_to_yaml(self._service._resolve_context_limits(repo, branch))
        effective = ContextLimits.from_review_yaml(
            {"context_limits": _merge(base, limits)}
        )
        pack = self._components.retriever.search_base(
            repo,
            query,
            limits=effective.search_codebase,
            hops=effective.graph.hops,
            ceiling_override=None,
            branch=branch,
            include_tests=False,
        )
        return pack.as_context(line_numbers=True) or "(ничего не найдено)"

    def code_multi(self, repo: str, branch: str, queries: list, limits: dict | None,
                   *, similar_paths: bool = False) -> str:
        """Мультизапросная выдача тем же продакшн-путём, что видит сборщик брифа.

        Подмешанный сигнал (PRI-257, только similar-diffs — co-change снят
        по итогам приёмки) собирается продакшн-объектом _TaskContextDeps: своей
        копии сборки путей здесь не заводится, иначе replay мерил бы не тот
        вход, что видит прод. Эффективные лимиты считаются ДО сборки
        источников (иначе оверрайд --set не доехал бы до квоты и сторона «до»
        замера совпала бы со стороной «после» — тот же дефект, что чинил
        PRI-256 для section_limits).
        """
        base = limits_to_yaml(self._service._resolve_context_limits(repo, branch))
        effective = ContextLimits.from_review_yaml(
            {"context_limits": _merge(base, limits or {})}
        )
        sources = []
        if similar_paths:
            from reviewer.mcp.service import _TaskContextDeps
            from reviewer.retrieval.augment import AugmentSource
            deps = _TaskContextDeps(self._service, None)
            # Хиты похожих задач наполняются вызовом similar; первый подзапрос —
            # это и есть продакшн-запрос задачи целиком (см. _queries).
            deps.similar(queries[0], None)
            sources.append(AugmentSource(name="similar-diffs", paths=deps._augment_paths(repo),
                                         quota=effective.code_section.max_augmented_files))
        if not limits:
            return self._service._search_codebase_multi(
                repo, list(queries), branch, False,
                augment_sources=sources or None)
        from reviewer.retrieval.multiquery import search_multi
        pack = search_multi(
            self._components.retriever, repo, list(queries),
            limits=effective.search_codebase,
            # Файловый бюджет секции (PRI-256) передаётся явно: без него оверрайд
            # code_section игнорировался бы молча и сторона «до» замера совпала
            # бы со стороной «после».
            section_limits=effective.code_section,
            hops=effective.graph.hops,
            branch=branch, include_tests=False,
            augment_sources=sources or None,
        )
        return pack.as_context(line_numbers=True) or "(ничего не найдено)"

    def neighbors(self, repo: str, branch: str, node_ids: list) -> set:
        """Соседи символов по исходящим CALLS/IMPLEMENTS — обход для ядра (PRI-261).

        Ввод-вывод живёт здесь, а не в reviewer/metrics/brief_quality: тот модуль
        обязан оставаться чистым, иначе офлайн и онлайн перестают мерить одной
        линейкой. Отсутствующий граф даёт пустое множество, а не отказ прогона.
        """
        graph = self._components.graph
        if graph is None or not node_ids:
            return set()
        return graph.outgoing_neighbors(repo, sorted(node_ids), branch=branch)


def open_live(repo: str | None = None, branch: str | None = None) -> tuple:
    """Собрать живой провайдер и вернуть (provider, repo, branch).

    repo по умолчанию — DEFAULT_REPO, ветка — первичная отслеживаемая.
    """
    settings = Settings()
    resolved_repo = repo or settings.default_repo
    if not resolved_repo:
        raise SystemExit(
            "не задан репозиторий: укажите --repo owner/name или DEFAULT_REPO"
        )
    resolved_branch = branch or resolve_repo_branches(
        resolved_repo, settings=settings
    ).primary
    components = build_components(settings)
    try:
        service = MCPReviewService(settings, components)
    except Exception:
        components.close()
        raise
    return LiveRetrieval(settings, components, service), resolved_repo, resolved_branch
