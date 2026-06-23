"""Сервисный слой MCP-сервера: prepare/search/publish поверх Components.

Состояние сессии (PreparedReview + ToolContext) живёт в процессе сервера
между вызовами prepare_review и publish_review одного PR.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from reviewer.agent.assemble import (
    AssembledReview,
    assemble_review,
    ground_line,
    snap_to_commentable,
)
from reviewer.agent.centrality import annotate_centrality
from reviewer.agent.dedup import dedup_findings
from reviewer.app import Components
from reviewer.config.settings import Settings
from reviewer.index.refs import base_ref
from reviewer.mcp.session_serde import from_payload, to_payload
from reviewer.mcp.session_store import SessionStore
from reviewer.retrieval.retriever import ContextPack
from reviewer.services.review_service import (
    BranchNotTrackedError,
    PreparedReview,
    ReviewService,
)
from reviewer.tasks.graph import PRRef
from reviewer.tools.code_tools import ToolContext, make_tools
from reviewer.tools.graph_format import format_neighbors
from reviewer.mcp.schemas import FindingIn, VerdictIn
from reviewer.vcs.base import ChangedFile, Finding, VCSProvider
from reviewer.vcs.diff import commentable_lines

log = logging.getLogger(__name__)

WALKTHROUGH_MARKER = "<!-- ai-walkthrough -->"


@dataclass
class _Session:
    prepared: PreparedReview
    # Храним ctx, а не готовые tools: make_tools(ctx) пересоздаётся на каждый
    # _invoke_tool-вызов, чтобы seen-дедуп (set внутри make_tools) сбрасывался
    # пер-вызов. Повторный одинаковый вызов отдаёт реальный результат из
    # ctx.cache (пер-сессия), а не заглушку «повтор: результат уже показан выше».
    ctx: ToolContext
    # PRI-156: schema-enforced находки/вердикты копятся в сессии между submit_*
    # и publish_review. id вида "f{n}" присваивает submit_findings. Состояние
    # in-memory (регидрированная из стора сессия стартует пустой — допустимо:
    # перезапуск процесса посреди ревью теряет прогресс, как и раньше).
    candidates: dict[str, Finding] = field(default_factory=dict)
    verdicts: dict[str, bool] = field(default_factory=dict)
    _seq: int = 0


class MCPReviewService:
    """Сервисный слой MCP-сервера: управляет сессиями PR и делегирует инструменты.

    Не потокобезопасен; рассчитан на последовательное исполнение sync-тулов
    FastMCP (sync-функции исполняются в event loop без конкуренции).
    При переводе тулов на async/to_thread потребуется защита _sessions.
    """

    def __init__(
        self,
        settings: Settings,
        components: Components,
        vcs_factory: Callable[[str, str], VCSProvider] | None = None,
    ) -> None:
        self.settings = settings
        self.components = components
        self._review_service = ReviewService(settings, components)
        self._vcs_factory = vcs_factory  # для тестов; None = GitHubProvider
        self._sessions: dict[tuple[str, int], _Session] = {}
        self._session_store: SessionStore | None = None

    def prepare_review(self, repo: str, pr: int) -> dict:
        """Подготовить ревью PR: получить юниты, policy, patches; сохранить сессию.

        При повторном вызове для того же (repo, pr) сессия перезаписывается;
        внутренне созданный VCS старой сессии (без vcs_factory) при этом
        fail-soft закрывается — иначе httpx-клиент утёк бы в долгоживущем
        сервере. Жизненным циклом factory-созданных провайдеров владеет
        фабрика (vcs_factory test-only).
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        owner, name = repo.split("/", 1)
        old = self._sessions.get((repo, pr))
        vcs = self._vcs_factory(owner, name) if self._vcs_factory else None
        try:
            prepared = self._review_service.prepare(owner, name, pr, vcs_provider=vcs)
        except BranchNotTrackedError as e:
            log.info("Ревью %s#%s пропущено: ветка '%s' не отслеживается",
                     repo, pr, e.branch)
            return {"status": "skipped",
                    "reason": f"branch '{e.branch}' not tracked (REVIEW_BRANCHES)"}
        ctx = self._tool_context(prepared)
        self._sessions[(repo, pr)] = _Session(prepared, ctx)
        store = self._ensure_session_store()
        if store is not None:
            store.save(repo, pr, to_payload(prepared))
        # Старую сессию прибираем ПОСЛЕ успешного prepare: при сбое повторной
        # подготовки старая сессия остаётся в _sessions, но её overlay уже удалён
        # self-healing'ом в начале prepare — последующие tool-вызовы по ней
        # вернут только base-данные (без overlay PR).
        # Закрываем только внутренне созданный провайдер.
        if old is not None and self._vcs_factory is None:
            try:
                old.prepared.vcs.close()
            except Exception:
                log.warning(
                    "Не удалось закрыть VCS-провайдер старой сессии %s#%s",
                    repo, pr, exc_info=True,
                )
        return self._prepared_payload(prepared)

    def _tool_context(self, prepared: PreparedReview) -> ToolContext:
        """Построить ToolContext из PreparedReview — аналогично _LLMPhase._make_tool_context."""
        return ToolContext(
            retriever=self.components.retriever,
            graph=self.components.graph,
            overlay_ref=prepared.overlay_ref,
            changed_paths=prepared.changed_paths,
            changed_node_ids=prepared.changed_node_ids,
            repo=prepared.repo,
            branch=prepared.branch,
            read_file_fn=(
                (lambda p: prepared.vcs.get_file_at_ref(p, prepared.prq.head_sha))
                if prepared.vcs else None
            ),
            patches=prepared.patches,
            store=getattr(self.components.retriever, "store", None),
            cache={},
        )

    def _ensure_session_store(self) -> SessionStore | None:
        """Ленивое хранилище подложки сессий (по образцу _ensure_history).

        Возвращает None (персист выключен), если ``review_session_persist`` ложно
        ИЛИ задан ``_vcs_factory`` (test-only: после рестарта фабрика недоступна,
        регидрация подняла бы реальный GitHubProvider — неверно для снапшота).
        Уже внедрённый ``_session_store`` (в тестах) возвращается как есть.
        """
        if self._session_store is not None:
            return self._session_store
        if self.settings.review_session_persist and self._vcs_factory is None:
            self._session_store = SessionStore(
                self.settings.pg_dsn,
                min_size=self.settings.pg_pool_min_size,
                max_size=self.settings.pg_pool_max_size,
            )
        return self._session_store

    def _rehydrate_session(self, repo: str, pr: int) -> _Session | None:
        """Восстановить сессию из Postgres при промахе in-memory кэша.

        Возвращает None, если персист выключен, строки нет/истёк TTL, БД
        недоступна или payload несовместим (fail-soft → вызывающий бросит
        ValueError с recovery hint).

        Прогрев кэша `_sessions` — ответственность вызывающего (`_session`);
        метод только загружает и собирает сессию, не мутируя кэш.
        """
        store = self._ensure_session_store()
        if store is None:
            return None
        payload = store.load(repo, pr, self.settings.review_session_ttl_hours)
        if not payload:
            return None
        try:
            owner, name = repo.split("/", 1)
            vcs = self._review_service._create_vcs_provider(owner, name)
            prepared = from_payload(payload, vcs)
        except Exception:
            log.warning("Регидрация сессии %s#%s не удалась", repo, pr, exc_info=True)
            return None
        ctx = self._tool_context(prepared)
        return _Session(prepared, ctx)

    def _session(self, repo: str, pr: int) -> _Session:
        """Получить сессию из кэша или регидрировать из Postgres (crash-recovery).

        При промахе in-memory кэша пробуем поднять персистнутую сессию; успех
        прогревает кэш. Полный промах (нет строки / истёк TTL / БД недоступна) —
        ValueError с recovery hint.
        """
        s = self._sessions.get((repo, pr))
        if s is not None:
            return s
        rehydrated = self._rehydrate_session(repo, pr)
        if rehydrated is not None:
            self._sessions[(repo, pr)] = rehydrated
            return rehydrated
        raise ValueError(
            f"Сессия для {repo}#{pr} не найдена или истекла — вызови prepare_review заново"
        )

    def _invoke_tool(self, repo: str, pr: int, name: str, args: dict) -> str:
        """Вызов инструмента с per-вызов пересозданием make_tools.

        seen-дедуп сбрасывается на каждый вызов (повтор отдаёт результат из
        ctx.cache, а не заглушку «повтор»); сам кэш живёт всю сессию.
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        tools = {t.name: t for t in make_tools(s.ctx)}
        return tools[name].invoke(args)

    def search_code(self, repo: str, pr: int, query: str) -> str:
        """Семантико-лексический поиск кода по индексу PR."""
        return self._invoke_tool(repo, pr, "search_code", {"query": query})

    def get_related_symbols(self, repo: str, pr: int, node_id: str) -> str:
        """Связанные символы (вызовы/реализации/тесты) для node_id вида 'path#fqn'."""
        return self._invoke_tool(repo, pr, "get_related_symbols", {"node_id": node_id})

    def read_file(self, repo: str, pr: int, path: str, start: int = 1, end: int = 400,
                  skeleton: bool = False) -> str:
        """Исходник файла на head-ревизии PR, строки [start..end].

        При skeleton=True — AST-скелет (сигнатуры def/class + 1-я строка docstring),
        start/end игнорируются. Дефолты start/end синхронизированы с code_tools.read_file.
        """
        return self._invoke_tool(repo, pr, "read_file",
                                 {"path": path, "start": start, "end": end, "skeleton": skeleton})

    def get_definition(self, repo: str, pr: int, symbol: str) -> str:
        """Где определён символ + его исходный код."""
        return self._invoke_tool(repo, pr, "get_definition", {"symbol": symbol})

    def find_callers(self, repo: str, pr: int, node_id: str) -> str:
        """Кто вызывает символ node_id ('path#fqn') — направленный CALLS (impact-анализ)."""
        return self._invoke_tool(repo, pr, "find_callers", {"node_id": node_id})

    def get_changed_file_diff(self, repo: str, pr: int, path: str) -> str:
        """Дифф другого изменённого файла этого PR."""
        return self._invoke_tool(repo, pr, "get_changed_file_diff", {"path": path})

    def get_impact(self, repo: str, pr: int) -> str:
        """Радиус поражения PR: изменённые сигнатуры → вызывающие вне диффа (impact-анализ)."""
        return self._invoke_tool(repo, pr, "get_impact", {})

    def index_task(self, task: dict) -> dict:
        """Проиндексировать нормализованный TaskBrief: эмбеддинг + граф задачи."""
        return self.components.task_service.index_task(task)

    def index_tasks_batch(self, tasks: list[dict]) -> list[dict]:
        """Батчевая индексация списка TaskBrief: один Voyage-вызов для изменившихся задач."""
        return self.components.task_service.index_batch(tasks)

    def search_tasks(self, query: str, top_k: int = 5) -> str:
        """Похожие по смыслу задачи (гибрид-поиск по корпусу задач)."""
        return self.components.task_service.search_tasks(query, top_k)

    def get_task_context(self, key: str) -> str:
        """Граф-контекст задачи: связанные задачи → их PR → затронутый код."""
        return self.components.task_service.get_task_context(key)

    def get_task(self, key: str) -> dict | None:
        """Нормализованный TaskBrief задачи из стора (store-first /solve-task).

        В отличие от get_task_context (граф: связи/PR/код) — это собственный контент
        задачи (title/description/status/url) из Postgres. None, если задачи нет в сторе.
        """
        return self.components.task_service.get_task(key)

    def board_config(self) -> dict:
        """Глобальный (env) конфиг доски задач деплоя — для клиентских скилов.

        Скилы sync-tasks/solve-task сначала читают per-repo .review.yml; если
        там нет блока task_board, берут этот глобальный дефолт, чтобы не плодить
        .review.yml в каждом репозитории. ``{"task_board": None}`` = доска в
        деплое не настроена (задайте TASK_BOARD_* в .env reviewer-mcp).
        """
        return {"task_board": self.settings.task_board_default()}

    def purge_orphaned_tasks(
        self, active_keys: list[str], keep_with_prs: bool = True
    ) -> dict:
        """Удалить осиротевшие задачи из store и графа."""
        return self.components.task_service.purge_orphaned_tasks(
            active_keys, keep_with_prs=keep_with_prs
        )

    def sync_board(self, board: str | None = None, limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True) -> dict:
        """Server-side ETL: перечислить доску по REST, нормализовать, проиндексировать.

        Доска/ключ не настроены → понятный error-summary (fail-soft), без падения.
        """
        sync = getattr(self.components, "sync_service", None)
        if sync is None:
            return {"status": "error",
                    "reason": "task board REST not configured — set TASK_BOARD_TYPE + "
                              "TASK_BOARD_API_KEY in the reviewer-mcp env "
                              "(~/.config/rag-reviewer/.env), then reconnect. Yougile key: "
                              "configurator (Ctrl+~ → API) or POST /api-v2/auth/keys"}
        try:
            return sync.run(board=board, limit=limit,
                            purge_orphaned=purge_orphaned,
                            keep_with_prs=keep_with_prs)
        except Exception as e:
            log.warning("sync_board: сбой синка", exc_info=True)
            return {"status": "error", "reason": f"{type(e).__name__}: {e}"}

    def _resolve_repo_branch(self, repo: str, branch: str | None) -> tuple[str, str] | str:
        """Резолв (repo, ветка) для session-less тулов.

        Возвращает (normalized_repo, resolved_branch) при успехе либо строку-заметку
        об ошибке (её тул отдаёт пользователю как есть). Ветка валидируется по
        REVIEW_BRANCHES; пустая ветка → первичная.
        """
        from reviewer.services.repo_id import normalize_repo
        raw = repo or self.settings.default_repo
        if not raw:
            return "(repo не задан: передайте repo или задайте DEFAULT_REPO)"
        try:
            repo = normalize_repo(raw)
        except ValueError:
            return f"(некорректный repo: {raw!r})"
        if branch and branch not in self.settings.review_branches_list():
            return (f"(ветка {branch!r} не в REVIEW_BRANCHES "
                    f"({self.settings.review_branches_list()}))")
        return (repo, branch or self.settings.primary_branch())

    def _resolve_summary_depth(self, repo: str, branch: str) -> tuple[int, str]:
        """Резолв глубины кластеризации сводок: env-дефолт → override из .review.yml ветки.

        repo уже нормализован (вызывается после _resolve_repo_branch). Fail-soft:
        нет токена/ветки/файла/кривой yml → (settings.summary_cluster_depth, "env").
        Внутренне созданный VCS-провайдер закрываем в finally (как get_pr_diff).
        source = ".review.yml", только если файл явно задаёт ключ summary_cluster_depth."""
        import yaml
        from reviewer.policy.policy import ReviewPolicy
        default = self.settings.summary_cluster_depth
        owner, name = repo.split("/", 1)
        vcs = None
        try:
            vcs = (self._vcs_factory(owner, name) if self._vcs_factory
                   else self._review_service._create_vcs_provider(owner, name))
            text = vcs.get_file_at_ref(".review.yml", branch)
            if not text:
                return default, "env"
            data = yaml.safe_load(text) or {}
            depth = ReviewPolicy.load(self.settings, text).summary_cluster_depth
            return depth, (".review.yml" if "summary_cluster_depth" in data else "env")
        except Exception:
            log.warning("_resolve_summary_depth: fail-soft → env-дефолт", exc_info=True)
            return default, "env"
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("_resolve_summary_depth: не удалось закрыть VCS", exc_info=True)

    def _resolve_summary_topk_threshold(self, repo: str, branch: str) -> tuple[int, str]:
        """Резолв порога масштаба приора сводок: env-дефолт → override из .review.yml ветки.

        repo уже нормализован (вызывается после _resolve_repo_branch). Fail-soft:
        нет токена/ветки/файла/кривой yml → (settings.summary_topk_threshold, "env").
        source = ".review.yml", только если файл явно задаёт ключ summary_topk_threshold."""
        import yaml
        from reviewer.policy.policy import ReviewPolicy
        default = self.settings.summary_topk_threshold
        owner, name = repo.split("/", 1)
        vcs = None
        try:
            vcs = (self._vcs_factory(owner, name) if self._vcs_factory
                   else self._review_service._create_vcs_provider(owner, name))
            text = vcs.get_file_at_ref(".review.yml", branch)
            if not text:
                return default, "env"
            data = yaml.safe_load(text) or {}
            val = ReviewPolicy.load(self.settings, text).summary_topk_threshold
            return val, (".review.yml" if "summary_topk_threshold" in data else "env")
        except Exception:
            log.warning("_resolve_summary_topk_threshold: fail-soft → env-дефолт", exc_info=True)
            return default, "env"
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("_resolve_summary_topk_threshold: не удалось закрыть VCS",
                                exc_info=True)

    def search_codebase(self, repo: str, query: str, top_k: int = 10,
                        branch: str | None = None,
                        include_tests: bool = False) -> str:
        """Гибрид-поиск по base-индексу репозитория (без PR-сессии) — для /solve-task.

        branch — отслеживаемая ветка (allowlist REVIEW_BRANCHES); по умолчанию
        первичная. Поиск идёт по индексу указанной ветки (base:<branch>).
        Выдача: без вложенных дублей и (по умолчанию) без тест-чанков, с
        построчными номерами для цитирования path:line без повторного Read.
        include_tests=True возвращает тест-чанки.
        """
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        try:
            pack = self.components.retriever.search_base(
                repo, query, top_k=top_k, branch=resolved, include_tests=include_tests)
        except Exception:
            log.warning("search_codebase: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
        return pack.as_context(line_numbers=True) or "(ничего не найдено)"

    def related_symbols(self, repo: str, node_id: str,
                        branch: str | None = None) -> str:
        """Соседи символа по графу (calls/implements/tests) без PR-сессии.
        На элемент: file:line + строка определения + тип ребра и дистанция."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        if self.components.graph is None:
            return "(граф недоступен)"
        try:
            neighbors = self.components.graph.expand_detailed(
                repo, [node_id], hops=2, branch=resolved)
        except Exception:
            log.warning("related_symbols: сбой графа", exc_info=True)
            return "(нет связей)"
        return format_neighbors(
            neighbors, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(нет связей)")

    def callers(self, repo: str, node_id: str,
                branch: str | None = None) -> str:
        """Кто вызывает символ node_id ('path#fqn') — входящие CALLS, без PR-сессии.
        На элемент: file:line + строка определения вызывающего + [CALLS]."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        if self.components.graph is None:
            return "(граф недоступен)"
        try:
            found = self.components.graph.callers_detailed(
                repo, [node_id], branch=resolved)
        except Exception:
            log.warning("callers: сбой графа", exc_info=True)
            return "(вызовов не найдено)"
        return format_neighbors(
            found, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)")

    def definition(self, repo: str, symbol: str,
                   branch: str | None = None) -> str:
        """Где определён символ + исходник (граф → индекс → семантический фолбэк),
        без PR-сессии. Тесты не отфильтровываются — символ может быть тестом."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        try:
            ids: list[str] = []
            if self.components.graph is not None:
                ids = self.components.graph.find_symbol(
                    repo, symbol, branch=resolved)
            if ids:
                nodes = self.components.store.fetch_nodes(
                    repo, ids[:3], None, [], base_ref=base_ref(resolved))
                if nodes:
                    return ContextPack(items=nodes).as_context(line_numbers=True)
            pack = self.components.retriever.search_base(
                repo, symbol, top_k=3, branch=resolved, include_tests=True)
            return pack.as_context(line_numbers=True) or "(определение не найдено)"
        except Exception:
            log.warning("definition: сбой", exc_info=True)
            return "(определение не найдено)"

    def list_subsystem_clusters(self, repo: str, branch: str | None = None,
                                depth: int | None = None, min_size: int | None = None,
                                cap: int | None = None) -> dict:
        """Кластеризовать base-граф по модулям → кластеры для /summarize-subsystems.
        cap (дефолт Settings.summary_rebuild_cap; None/0=безлимит) отбрасывает наименее
        приоритетные stale-кластеры (без сводки → старейшие updated_at первыми) и считает
        их в deferred (PRI-165)."""
        from reviewer.graph.summaries import Member, build_clusters
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"branch": branch or "", "deferred": 0, "clusters": [], "note": rb}
        repo, resolved = rb
        raw = self.components.store.list_base_members(repo, resolved)
        if not raw:
            return {"branch": resolved, "deferred": 0, "clusters": [],
                    "note": "(base-индекс пуст — выполните /reviewer_sync-codebase)"}
        members = [Member(node_id=f"{p}#{s}", path=p, content_hash=h, start_line=sl,
                          skeleton_hash=sk)
                   for p, s, h, sl, sk in raw]
        graph = self.components.graph
        in_degree_fn = (
            (lambda ids: graph.in_degree(repo, ids, branch=resolved))
            if graph is not None else None)
        if depth is None:
            resolved_depth, depth_source = self._resolve_summary_depth(repo, resolved)
        else:
            resolved_depth, depth_source = depth, "arg"
        clusters = build_clusters(
            members, in_degree_fn, depth=resolved_depth, min_size=min_size or 1)
        stored = self.components.summary_store.get_source_hashes(repo, resolved)
        stale = {c.key: (stored.get(c.key) != c.source_hash) for c in clusters}
        orphans = len(set(stored) - {c.key for c in clusters})
        effective_cap = cap if cap is not None else self.settings.summary_rebuild_cap
        deferred_keys: set[str] = set()
        if effective_cap and effective_cap > 0:
            stale_cl = [c for c in clusters if stale[c.key]]
            if len(stale_cl) > effective_cap:
                updated = self.components.summary_store.get_updated_ats(repo, resolved)
                never = [c for c in stale_cl if c.key not in updated]      # без сводки — первыми
                aged = sorted((c for c in stale_cl if c.key in updated),
                              key=lambda c: updated[c.key])                # старейшие — раньше
                deferred_keys = {c.key for c in (never + aged)[effective_cap:]}
        return {"branch": resolved, "depth": resolved_depth, "depth_source": depth_source,
                "deferred": len(deferred_keys), "orphans": orphans, "clusters": [
            {"cluster_key": c.key, "num_members": c.num_members, "files": c.files,
             "top_symbols": c.top_symbols, "source_hash": c.source_hash,
             "stale": stale[c.key]}
            for c in clusters if c.key not in deferred_keys]}

    def index_subsystem_summary(self, repo: str, branch: str, cluster_key: str,
                                title: str, summary: str, source_hash: str) -> dict:
        """Персистнуть один summary подсистемы (idempotent upsert).

        member_node_ids выводятся сервером (re-derive по cluster_key над base-составом)
        и пишутся только при совпадении пере-вычисленного source_hash с переданным —
        иначе [] + note (состав базы изменился между list и index; самозалечивается
        следующим проходом summarize-subsystems)."""
        from reviewer.graph.summaries import cluster_key as cluster_key_of, compute_source_hash
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"stored": False, "note": rb}
        repo, resolved = rb
        # depth резолвится тем же хелпером, что list_subsystem_clusters без явного depth:
        # cluster_key и source_hash зависят от depth, поэтому совпадение хешей гарантировано
        # только когда кластеры листались тем же дефолтом. При нестандартном depth —
        # fail-soft []+note ниже.
        depth, _ = self._resolve_summary_depth(repo, resolved)
        raw = self.components.store.list_base_members(repo, resolved)
        members = [(f"{p}#{s}", sk) for p, s, _h, _sl, sk in raw
                   if cluster_key_of(p, depth) == cluster_key]
        consistent = compute_source_hash(members) == source_hash
        member_node_ids = sorted(nid for nid, _ in members) if consistent else []
        # Дедуп эмбеддинга по source_hash (PRI-167): пересчитываем вектор только если
        # хеш кластера изменился; иначе embedding=None → COALESCE сохранит старый вектор,
        # Voyage не дёргается. Сбой Voyage → embedding=None + note (бэкфилл доберёт).
        note: str | None = None
        embedding: list[float] | None = None
        stored_hash = self.components.summary_store.get_source_hashes(repo, resolved).get(cluster_key)
        if stored_hash != source_hash:
            try:
                embedding = self.components.embedder.embed_documents([f"{title}\n{summary}"])[0]
            except Exception:
                log.warning("index_subsystem_summary: сбой эмбеддинга — бэкфилл доберёт",
                            exc_info=True)
                note = "эмбеддинг не вычислен (Voyage недоступен) — будет добран бэкфиллом"
        self.components.summary_store.upsert_summary(
            repo, resolved, cluster_key, title, summary, member_node_ids, source_hash,
            embedding=embedding)
        out = {"cluster_key": cluster_key, "stored": True, "members": len(member_node_ids)}
        if not consistent:
            out["note"] = "состав кластера изменился с момента list — member_node_ids не сохранены"
        elif note:
            out["note"] = note
        return out

    def get_subsystem_summaries(self, repo: str, branch: str | None = None,
                                cluster_key: str | None = None, query: str | None = None,
                                top_k: int | None = None) -> dict:
        """Дешёвый приор: предрасчитанные summary подсистем (fail-open у потребителя).

        cluster_key → одна сводка. Иначе: при query И числе сводок > порога масштаба
        (SUMMARY_TOPK_THRESHOLD, per-repo .review.yml) — ANN top-k по близости (PRI-167);
        иначе (без query или ≤ порога) — все (бэк-компат)."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"summaries": [], "note": rb}
        repo, resolved = rb
        store = self.components.summary_store
        if cluster_key:
            return {"summary": store.get_summary(repo, resolved, cluster_key)}
        if query:
            threshold, _ = self._resolve_summary_topk_threshold(repo, resolved)
            if store.count_summaries(repo, resolved) > threshold:
                qvec = self.components.embedder.embed_query(query)
                return {"summaries": store.search_summaries(repo, resolved, qvec, top_k or 8)}
        return {"summaries": store.get_summaries(repo, resolved)}

    def prune_subsystem_summaries(self, repo: str, branch: str | None = None) -> dict:
        """Удалить сводки подсистем, осиротевшие после смены depth или удаления модулей.

        Пере-выводит текущие cluster_keys из base-состава на резолвнутом depth и
        удаляет сводки вне этого множества. Вызывать ТОЛЬКО на полном (uncapped)
        прогоне скилла — иначе отложенные капом кластеры будут приняты за осиротевшие.
        Пустой base → no-op (не вайпать на транзиентной пустоте). Fail-soft."""
        from reviewer.graph.summaries import cluster_key as cluster_key_of
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"pruned": 0, "kept": 0, "note": rb}
        repo, resolved = rb
        depth, _ = self._resolve_summary_depth(repo, resolved)
        raw = self.components.store.list_base_members(repo, resolved)
        if not raw:
            return {"pruned": 0, "kept": 0, "note": "(base-индекс пуст — purge пропущен)"}
        keep_keys = sorted({cluster_key_of(p, depth) for p, _s, _h, _sl, _sk in raw})
        pruned = self.components.summary_store.delete_summaries_except(repo, resolved, keep_keys)
        return {"pruned": pruned, "kept": len(keep_keys)}

    def backfill_summary_embeddings(self, repo: str, branch: str | None = None) -> dict:
        """Self-heal: дозаполнить эмбеддинги сводок с embedding IS NULL из хранимого
        title+summary (без LLM, дедуп по NULL). Идемпотентно: следующий прогон → 0.
        Вызывается /summarize-subsystems после LLM-прохода. Fail-soft (PRI-167)."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"embedded": 0, "note": rb}
        repo, resolved = rb
        store = self.components.summary_store
        pending = store.get_pending_embeddings(repo, resolved)
        if not pending:
            return {"embedded": 0}
        try:
            vecs = self.components.embedder.embed_documents(
                [f"{p['title']}\n{p['summary']}" for p in pending])
        except Exception:
            log.warning("backfill_summary_embeddings: сбой эмбеддинга", exc_info=True)
            return {"embedded": 0, "note": "Voyage недоступен — бэкфилл пропущен"}
        for p, vec in zip(pending, vecs):
            store.set_embedding(repo, resolved, p["cluster_key"], vec)
        return {"embedded": len(pending)}

    def get_pr_diff(self, repo: str, number: int) -> str:
        """Unified diff изменённых файлов PR (session-less) — ленивая подтяжка для /solve-task.

        repo обязателен ("owner/name"): PR может быть в другом репозитории, граф
        задач глобален. Дифф усечён до _PR_DIFF_MAX_CHARS. Любая ошибка → fail-soft нота.
        """
        from reviewer.services.repo_id import normalize_repo
        raw = repo or self.settings.default_repo
        if not raw:
            return "(repo не задан: передайте repo или задайте DEFAULT_REPO)"
        try:
            repo = normalize_repo(raw)
        except ValueError:
            return f"(некорректный repo: {raw!r})"
        owner, name = repo.split("/", 1)
        # Создание провайдера ВНУТРИ try: сбой (плохой токен и т.п.) → fail-soft нота,
        # а не проброс. vcs=None до создания, чтобы finally не упал NameError'ом.
        vcs = None
        try:
            vcs = (self._vcs_factory(owner, name) if self._vcs_factory
                   else self._review_service._create_vcs_provider(owner, name))
            files = vcs.get_changed_files(number)
        except Exception:
            log.warning("get_pr_diff: сбой получения diff для %s#%s",
                        repo, number, exc_info=True)
            return "(diff PR недоступен)"
        finally:
            # Внутренне созданный провайдер закрываем сами (factory-владельца — нет).
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("get_pr_diff: не удалось закрыть VCS", exc_info=True)
        return _format_pr_diff(files) or "(PR без изменённых файлов)"

    def submit_findings(self, repo: str, pr: int, findings: list[dict]) -> dict:
        """Принять находки субагента в сессию (PRI-156): валидация по FindingIn,
        присвоение server-assigned id, накопление в _Session.candidates.

        Энфорс схемы — на тул-границе FastMCP (тип list[FindingIn]); здесь
        повторная model_validate коэрцирует/валидирует dict при прямом вызове
        (тесты). Невалидный элемент (нет file) → ValidationError → ретрай тула.
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        ids: list[str] = []
        for d in findings:
            fi = FindingIn.model_validate(d)
            s._seq += 1
            fid = f"f{s._seq}"
            s.candidates[fid] = Finding.from_in(fi)
            ids.append(fid)
        return {"accepted": len(ids), "ids": ids}

    def get_candidate_findings(self, repo: str, pr: int) -> str:
        """Вернуть накопленных кандидатов с id для verify (JSON-строка)."""
        import json
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        items = [
            {"id": fid, "file": f.file, "line": f.line, "category": f.category,
             "severity": f.severity, "message": f.message, "code_quote": f.code_quote}
            for fid, f in s.candidates.items()
        ]
        return json.dumps({"candidates": items}, ensure_ascii=False, indent=2)

    def submit_verdicts(self, repo: str, pr: int, verdicts: list[dict]) -> dict:
        """Принять вердикты verify в сессию (PRI-156). id вне candidates →
        игнор + warning. Отсутствие вердикта по находке = keep (см. publish_review)."""
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        recorded, unknown = 0, []
        for d in verdicts:
            v = VerdictIn.model_validate(d)
            if v.id not in s.candidates:
                unknown.append(v.id)
                log.warning("submit_verdicts: неизвестный id %s (%s#%s)", v.id, repo, pr)
                continue
            s.verdicts[v.id] = v.is_real
            recorded += 1
        return {"recorded": recorded, "unknown_ids": unknown}

    def publish_review(
        self,
        repo: str,
        pr: int,
        summary: str,
        dry_run: bool = False,
        task_key: str | None = None,
    ) -> dict:
        """Детерминированный хвост ревью: verify-фильтр → grounding → gate →
        dedup → assemble → публикация → история → очистка overlay/сессии.

        PRI-156: находки и вердикты читаются ИЗ СЕССИИ (submit_findings/
        submit_verdicts), параметр findings убран. Отсев verify — только по
        явному is_real=false; отсутствие вердикта = keep (recall-safe).
        Сессия (repo, pr) должна быть подготовлена prepare_review. Overlay и
        сессия очищаются ВСЕГДА (даже при сбое VCS-публикации) — см. _cleanup.

        При указании task_key и успешной публикации (не dry_run) линкует
        PR↔задача↔код в граф задач (рёбра IMPLEMENTED_BY + TOUCHES).

        Returns:
            Отчёт со счётчиками (posted/invalid/verify_rejected/dropped_by_gate/
            deduped/already_posted/moved_to_summary/capped) и inline.
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        p = s.prepared

        # 1) Verify-фильтр из сессии: keep, кроме явного is_real=false (recall-safe).
        # Затем грунтовка строки по дословной цитате (анти-галлюцинация).
        _commentable_cache: dict[str, dict[str, set[int]]] = {
            path: commentable_lines(patch)
            for path, patch in p.patches.items()
            if patch is not None
        }
        survived = [f for fid, f in s.candidates.items() if s.verdicts.get(fid) is not False]
        verify_rejected = len(s.candidates) - len(survived)
        parsed: list[Finding] = []
        for f in survived:
            f.line = ground_line(p.sources.get(f.file), f.code_quote, f.line)
            if f.line is not None and f.side == "RIGHT" and f.file in _commentable_cache:
                f.line = snap_to_commentable(
                    f.line, f.side, f.code_quote, _commentable_cache[f.file], p.sources.get(f.file, ""),
                    max_distance=p.policy.grounding_max_distance,
                )
            parsed.append(f)

        # 2) Gate (категория/severity/confidence/пути) + dedup.
        kept = [f for f in parsed if p.policy.gate(f)]
        deduped = dedup_findings(kept)

        # 2b) Центральность символа (граф) → tie-breaker сортировки в assemble (PRI-129).
        # Fail-soft: нет графа/стора/совпадений → centrality 0.0, порядок не меняется.
        annotate_centrality(
            deduped,
            self.components.graph,
            getattr(self.components.retriever, "store", None),
            repo=repo,
            branch=p.branch,
            changed_node_ids=p.changed_node_ids,
            overlay_ref=p.overlay_ref,
        )

        # 3) Существующие fingerprint'ы — для идемпотентности (fail-soft).
        try:
            existing = p.vcs.list_existing_fingerprints(pr)
        except Exception:
            log.warning("Не удалось получить существующие fingerprint", exc_info=True)
            existing = set()

        # 4) Сборка inline + markdown-сводки. assemble_review МУТИРУЕТ f.line —
        # после вызова f.fingerprint() согласован с findings_rows. patches
        # фильтруем по changed_paths (как в nodes.py).
        asm = assemble_review(
            deduped,
            patches={x: p.patches.get(x) for x in p.changed_paths},
            sources=p.sources,
            existing_fps=existing,
            max_comments=p.policy.max_comments,
            suggestions_mode=self._suggestions_mode(),
        )
        log.info(
            "publish_review %s pr:%s — размещено inline=%d, в сводку=%d, обрезано=%d",
            repo, pr, len(asm.inline_comments), asm.moved_to_summary, asm.capped,
        )
        full_summary = summary + ("\n\n" + asm.summary if asm.summary else "")

        # 5) Публикация (если не dry_run).
        error, posted = "", False
        if not dry_run:
            try:
                p.vcs.publish_review(pr, p.prq.head_sha, full_summary, asm.inline_comments)
                posted = True
            except Exception as e:
                log.error("Не удалось опубликовать ревью", exc_info=True)
                error = f"{type(e).__name__}: {e}"

        # 5b) Авто-линковка PR↔задача↔код в граф задач (реальная публикация).
        # Граф недоступен / сбой — fail-soft внутри link_review, ревью не падает.
        if not dry_run and posted and task_key:
            pr_ref = PRRef(
                repo=repo,
                number=pr,
                url=f"https://github.com/{repo}/pull/{pr}",
                sha=p.prq.head_sha,
            )
            self.components.task_service.link_review(
                task_key, pr_ref, p.changed_node_ids,
            )

        # 6) История (fail-soft) и очистка overlay/сессии (ВСЕГДА).
        dropped_by_gate = len(parsed) - len(kept)
        run_id = self._record_history(
            repo, pr, p, list(s.candidates.values()), deduped, asm,
            verify_rejected=verify_rejected,
            dry_run=dry_run, posted=posted, error=error,
        )
        self._cleanup(repo, pr)

        return {
            "posted": posted,
            "dry_run": dry_run,
            "error": error,
            "run_id": run_id,
            "summary": full_summary,
            "inline": [
                {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
                for c in asm.inline_comments
            ],
            "invalid": 0,                       # PRI-156: вход валиден по схеме (submit)
            "verify_rejected": verify_rejected,
            "dropped_by_gate": dropped_by_gate,
            "deduped": len(kept) - len(deduped),
            "already_posted": asm.skipped_existing,
            "moved_to_summary": asm.moved_to_summary,
            "capped": asm.capped,
        }

    def post_pr_walkthrough(self, repo: str, pr: int, markdown: str) -> dict:
        """Опубликовать walkthrough-гид в PR как review-комментарий (без inline-находок).

        Маркер ``<!-- ai-walkthrough -->`` в body отделяет гид от ревью-находок
        (``<!-- ai-review:* -->``). Outward-facing — вызывается только по явной
        просьбе пользователя. Fail-soft при сетевой ошибке."""
        from reviewer.services.repo_id import normalize_repo
        sess = self._session(normalize_repo(repo), pr)
        prepared = sess.prepared
        body = f"{WALKTHROUGH_MARKER}\n\n{markdown}"
        try:
            prepared.vcs.publish_review(pr, prepared.prq.head_sha, body, [])
        except Exception as e:
            log.warning("post_pr_walkthrough: сбой постинга", exc_info=True)
            return {"posted": False, "reason": f"{type(e).__name__}: {e}"}
        return {"posted": True, "pr": pr}

    def _record_history(
        self,
        repo: str,
        pr: int,
        p: PreparedReview,
        analyzed: list[Finding],
        deduped: list[Finding],
        asm: AssembledReview,
        *,
        verify_rejected: int,
        dry_run: bool,
        posted: bool,
        error: str,
    ) -> int | None:
        """Записать прогон в историю (fail-soft).

        Гейтится ``settings.review_history``. Любая ошибка истории не валит
        publish — возвращаем None. Стоимость/usage недоступны на этом этапе
        (LLM-вызовы прошли вне сервиса), пишем None. При сбое публикации
        (status=error) находки записываются с published=False — как в
        review_service._record_history.
        """
        try:
            history = self._review_service._ensure_history()
            if history is None:
                return None
            now = datetime.now(timezone.utc)
            status = "error" if (error and not dry_run) else "ok"
            comments_inline = len(asm.inline_comments)
            # PRI-156: analyzed — все candidates (до verify-фильтра); грунтовка строк
            # выполнена только для survived, не для отвергнутых candidates.
            findings_analyzed = len({f.fingerprint() for f in analyzed})
            run = {
                "repo": repo,
                "pr_number": pr,
                "base_sha": p.prq.base_sha,
                "head_sha": p.prq.head_sha,
                "model": "claude-code",
                "model_verify": None,
                "dry_run": dry_run,
                "started_at": now,
                "finished_at": now,
                "duration_ms": 0,
                "status": status,
                "files_reviewed": len(p.units),
                "files_skipped": len(p.skipped_paths or []),
                "files_failed": 0,
                "findings_analyzed": findings_analyzed,
                "findings_kept": len(deduped),
                # PRI-156: verify_rejected = число is_real=false (verify живёт в
                # сессии submit_verdicts); gate-отсев отдельно в отчёте publish.
                "verify_rejected": verify_rejected,
                "comments_inline": comments_inline,
                # Считаем только реально опубликованные не-inline строки
                # (findings_rows включает skipped_existing с published=False).
                "comments_summary": sum(
                    1 for r in asm.findings_rows if r["published"] and not r["inline"]
                ),
                "usage": None,
                "total_cost": None,
                "error_text": error or None,
            }
            rows = (
                [dict(r, published=False) for r in asm.findings_rows]
                if status == "error" else asm.findings_rows
            )
            return history.record_run(run, rows, steps=None)
        except Exception:
            log.warning("Не удалось сохранить историю прогона", exc_info=True)
            return None

    def _cleanup(self, repo: str, pr: int) -> None:
        """Закрыть сессию (repo, pr) и удалить эфемерный overlay pr:N (fail-soft).

        Внутренне созданный VCS-провайдер (vcs_factory is None) закрываем сами —
        иначе утечка httpx-клиента в долгоживущем сервере. factory-провайдером
        владеет фабрика, его не трогаем.
        """
        sess = self._sessions.pop((repo, pr), None)
        if sess is not None and self._vcs_factory is None:
            try:
                sess.prepared.vcs.close()   # внутренний провайдер — наш, закрываем
            except Exception:
                log.warning("Не удалось закрыть VCS-провайдер", exc_info=True)
        try:
            self.components.store.delete_ref(repo, f"pr:{pr}")
        except Exception:
            log.warning("Не удалось очистить overlay %s pr:%s", repo, pr, exc_info=True)
        store = self._ensure_session_store()
        if store is not None:
            store.delete(repo, pr)

    def _prepared_payload(self, p: PreparedReview) -> dict:
        """Сериализовать PreparedReview в dict для передачи MCP-клиенту."""
        units = []
        for u in p.units:
            lines = commentable_lines(p.patches.get(u.path))
            unit = {
                "path": u.path,
                "patch": p.patches.get(u.path),
                "commentable_right": sorted(lines["RIGHT"]),
                "commentable_left": sorted(lines["LEFT"]),
            }
            if u.structural_summary:
                unit["structural_summary"] = u.structural_summary
            units.append(unit)
        return {
            "repo": p.repo,
            "pr": {
                "number": p.prq.number,
                "title": p.prq.title,
                "body": p.prq.body,
                "base_sha": p.prq.base_sha,
                "head_sha": p.prq.head_sha,
                "base_ref": p.prq.base_ref,
                "draft": p.prq.draft,
            },
            "policy": {
                "severity_threshold": p.policy.severity_threshold,
                "min_confidence": p.policy.min_confidence,
                "max_comments": p.policy.max_comments,
                "categories": p.policy.categories,
                "enabled_only": p.policy.enabled_only,
                "ignore": p.policy.ignore,
                "output_language": p.policy.output_language,
            },
            "units": units,
            "task_board": p.task_board,
            "task_keys": p.task_keys,
            "skipped_paths": p.skipped_paths,
            "skip_drafts": self.settings.review_skip_drafts,
            "suggestions_mode": self._suggestions_mode(),
        }

    def _suggestions_mode(self) -> str:
        """Режим предложений (apply/text) — передаётся в assemble_review для сборки suggestion-блоков."""
        return self.settings.review_suggestions


_PR_DIFF_MAX_CHARS = 20000


def _format_pr_diff(files: list[ChangedFile]) -> str:
    """Список ChangedFile → текстовый unified-diff с символьным капом."""
    blocks: list[str] = []
    for f in files:
        head = f"--- {f.path} [{f.status}]"
        if f.patch is None:
            blocks.append(f"{head}\n(patch недоступен: файл слишком большой или бинарный)")
        else:
            blocks.append(f"{head}\n{f.patch}")
    out = "\n\n".join(blocks)
    if len(out) > _PR_DIFF_MAX_CHARS:
        out = out[:_PR_DIFF_MAX_CHARS] + "\n… (truncated)"
    return out
