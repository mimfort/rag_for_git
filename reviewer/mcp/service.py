"""Сервисный слой MCP-сервера: prepare/search/publish поверх Components.

Состояние сессии (PreparedReview + ToolContext) живёт в процессе сервера
между вызовами prepare_review и publish_review одного PR.
"""
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from reviewer.agent.assemble import (
    AssembledReview,
    assemble_review,
    ground_line,
    snap_to_commentable,
)
from reviewer.agent.centrality import annotate_centrality
from reviewer.agent.dedup import dedup_findings
from reviewer.agent.outcomes import account_outcomes
from reviewer.app import Components
from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.config.task_board import migrate_legacy_board_args, normalize_task_sync_filter
from reviewer.index.refs import base_ref
from reviewer.mcp.schemas import FindingIn, SummaryFragmentIn, VerdictIn
from reviewer.mcp.session_serde import from_payload, to_payload
from reviewer.mcp.session_store import SessionStore
from reviewer.retrieval.retriever import ContextPack
from reviewer.services.gc import purge_orphaned_overlays
from reviewer.services.review_service import (
    BranchNotTrackedError,
    PreparedReview,
    ReviewService,
)
from reviewer.services.summary_fragments import (
    FragmentDelta,
    FragmentFile,
    StoredSummaryFragment,
    build_fragment_delta,
    has_complete_fragment_generation,
    with_server_generation_provenance,
)
from reviewer.tasks.boards.base import (
    JsonValue,
    NativeSubtaskIdentity,
    RawTask,
    TaskBoardProvider,
)
from reviewer.tasks.boards.errors import (
    BoardProviderError,
    sanitize_provider_payload,
    sanitize_provider_text,
)
from reviewer.tasks.boards.registry import BoardProviderRegistry, default_board_registry
from reviewer.tasks.boards.runtime import resolved_provider
from reviewer.tasks.graph import PRRef
from reviewer.tasks.subtasks import (
    ConfirmedSubtaskIdentityError,
    SubtaskBatchResult,
    SubtaskRequest,
    WriteThroughResult,
    validate_confirmed_subtask_identities,
    validate_subtask_request,
)
from reviewer.tasks.sync import SyncProvider, SyncService
from reviewer.tasks.taskdoc import TaskDoc, render_markdown
from reviewer.tools.code_tools import ToolContext, make_tools
from reviewer.tools.graph_format import format_neighbors
from reviewer.vcs.base import ChangedFile, Finding, VCSProvider
from reviewer.vcs.diff import commentable_lines

if TYPE_CHECKING:
    from reviewer.config.layers import ResolutionMeta
    from reviewer.graph.summaries import Cluster, Member
    from reviewer.policy.context_limits import ContextLimits
    from reviewer.policy.policy import ReviewPolicy

log = logging.getLogger(__name__)

WALKTHROUGH_MARKER = "<!-- ai-walkthrough -->"

# PRI-209: ограничиваем размер трейса сессии в памяти, чтобы избежать
# неограниченного роста при длинных прогонах с большим числом tool calls.
_MAX_SESSION_STEPS = 1000

# PRI-212: DB-touch живости сессии (SessionStore.touch) — не чаще раза в
# _TOUCH_INTERVAL_S. Тулы зовутся LLM-темпом; при TTL в часах минутная
# гранулярность ничего не теряет и убирает бессмысленно частые UPDATE.
_TOUCH_INTERVAL_S = 60
_SUBTASK_RECOVERY_WARNING = (
    "subtask operation failed; durable recovery used a safe fallback"
)

# PRI-245: get_file_skeletons — капы batch-тула без PR-сессии.
_MAX_SKELETON_PATHS = 25      # путей на один вызов get_file_skeletons
_MAX_SKELETON_LINES = 400     # строк скелета на файл — как у read_file


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
    # PRI: причины reject от верификатора (id → строка), параллельно verdicts.
    # In-memory, как candidates/verdicts (регидрированная сессия стартует пустой).
    verdict_reasons: dict[str, str] = field(default_factory=dict)
    # PRI-209: шаги тул-лупа агента (для трассировки/метрик сессии).
    steps: list[dict] = field(default_factory=list)
    # PRI-209: момент начала сессии review (для duration_ms в истории).
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # PRI-212: последняя активность сессии (keepalive) — бампается на каждом
    # обращении через _session(); по ней _gc_overlays фильтрует active_keys.
    # started_at остаётся чистым «моментом создания» (duration_ms в истории).
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # PRI-212: момент последнего DB-touch (троттлинг _TOUCH_INTERVAL_S).
    db_touched_at: datetime | None = None
    _seq: int = 0


@dataclass(frozen=True)
class _SummaryState:
    """Единый снимок текущего состояния file-level сводок."""

    depth: int
    layout_token: str
    depth_source: str
    members: list["Member"]
    clusters: list["Cluster"]
    file_fingerprints: dict[str, str]
    fragments: list[StoredSummaryFragment]
    completed_depth: int | None
    completed_layout: str | None

    @property
    def bootstrap(self) -> bool:
        return self.completed_layout is None

    @property
    def full_rebuild(self) -> bool:
        return (
            self.completed_layout is not None
            and self.completed_layout != self.layout_token
        )


@dataclass
class _RunMetadata:
    """Метаданные LLM-прохода, передаваемые из publish_review в _record_history.

    started_at нормализуется в publish_review (str → datetime), поэтому
    _record_history ожидает здесь ``datetime | None``.
    """
    model: str | None = None
    model_verify: str | None = None
    usage: dict | None = None
    total_cost: float | None = None
    started_at: datetime | None = None
    steps: list[dict] | None = None


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
        *,
        board_registry: BoardProviderRegistry | None = None,
        board_credentials: ProviderCredentialSource | None = None,
    ) -> None:
        self.settings = settings
        self.components = components
        self._review_service = ReviewService(settings, components)
        self._vcs_factory = vcs_factory  # для тестов; None = GitHubProvider
        self._board_registry = board_registry or default_board_registry()
        self._board_credentials = (
            board_credentials or ProviderCredentialSource.from_settings(settings)
        )
        self._sessions: dict[tuple[str, int], _Session] = {}
        self._session_store: SessionStore | None = None
        # PRI-239: порог шума канала репорта багов. Не более одного предложения на
        # симптом за жизнь процесса, отказ пользователя запоминается. In-memory —
        # ровно как сессии ревью: «сессия» здесь и есть процесс reviewer-mcp.
        self._bug_offered: set[str] = set()
        self._bug_declined: set[str] = set()

    def prepare_review(self, repo: str, pr: int) -> dict:
        """Подготовить ревью PR: получить юниты, policy, patches; сохранить сессию.

        При повторном вызове для того же (repo, pr) сессия перезаписывается;
        внутренне созданный VCS старой сессии (без vcs_factory) при этом
        fail-soft закрывается — иначе httpx-клиент утёк бы в долгоживущем
        сервере. Жизненным циклом factory-созданных провайдеров владеет
        фабрика (vcs_factory test-only).

        Ключ сессии резервируется в Postgres ДО вызова ReviewService.prepare
        (который строит overlay) — см. комментарий у session_store.save ниже.
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        owner, name = repo.split("/", 1)
        self._gc_overlays()
        old = self._sessions.get((repo, pr))
        # Резервация ключа сессии ДО сборки overlay (prepare ниже строит его через
        # ~секунды GitHub round-trip'ов — для PR из 20 файлов реально 2-6 секунд).
        # Без резервации overlay в это окно существует без «свидетельства о
        # жизни» в review_sessions, и GC ДРУГОГО процесса reviewer-mcp (общий
        # Postgres, но каждый stdio-клиент — свой процесс) счёл бы его сиротой
        # и снёс молча: ревью продолжится, но агент будет ретривить изменённые
        # файлы из base-индекса, то есть ревьюить СТАРУЮ версию кода. Пустой
        # payload безопасен как значение: _rehydrate_session трактует его как
        # промах (`if not payload`), а настоящий save ниже перезатрёт строку.
        session_store = self._ensure_session_store()
        if session_store is not None:
            session_store.save(repo, pr, {})
        vcs = self._vcs_factory(owner, name) if self._vcs_factory else None
        try:
            prepared = self._review_service.prepare(owner, name, pr, vcs_provider=vcs)
        except BranchNotTrackedError as e:
            # Резервация не пригодилась (ранний skip, overlay не строился) —
            # снимаем, иначе пустая строка провисит в review_sessions до TTL.
            if session_store is not None:
                session_store.delete(repo, pr)
            log.info("Ревью %s#%s пропущено: ветка '%s' не отслеживается",
                     repo, pr, e.branch)
            return {"status": "skipped",
                    "reason": f"branch '{e.branch}' is not tracked for this "
                              "repository (see `reviewer config show`)"}
        except Exception:
            # Любой другой сбой prepare (в т.ч. недостроенный overlay, который
            # ReviewService.prepare уже подчищает сам себе в своём except) —
            # резервация тоже снимается, исходная ошибка пробрасывается как есть.
            if session_store is not None:
                session_store.delete(repo, pr)
            raise
        ctx = self._tool_context(prepared)
        # started_at проставляет default_factory поля _Session.started_at
        # (datetime.now(timezone.utc)) — явный аргумент здесь был бы дублем.
        self._sessions[(repo, pr)] = _Session(prepared, ctx)
        if session_store is not None:
            session_store.save(repo, pr, to_payload(prepared))
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
        # При регидратации started_at и steps сбрасываются — восстановленная
        # сессия начинает новый отсчёт времени жизни и не тащит чужую трассировку.
        return _Session(prepared, ctx)

    def _session(self, repo: str, pr: int) -> _Session:
        """Получить сессию из кэша или регидрировать из Postgres (crash-recovery).

        При промахе in-memory кэша пробуем поднять персистнутую сессию; успех
        прогревает кэш. Полный промах (нет строки / истёк TTL / БД недоступна) —
        ValueError с recovery hint.

        PRI-212 (keepalive): каждое обращение продлевает живость сессии —
        in-memory всегда (last_seen_at), в Postgres не чаще _TOUCH_INTERVAL_S
        (SessionStore.touch fail-soft: сбой БД не роняет обращение).
        """
        s = self._sessions.get((repo, pr))
        if s is None:
            s = self._rehydrate_session(repo, pr)
            if s is None:
                raise ValueError(
                    f"Сессия для {repo}#{pr} не найдена или истекла — вызови prepare_review заново"
                )
            self._sessions[(repo, pr)] = s
        self._touch_session(repo, pr, s)
        return s

    def _touch_session(self, repo: str, pr: int, s: _Session) -> None:
        """Продлить живость сессии активностью (PRI-212)."""
        now = datetime.now(timezone.utc)
        s.last_seen_at = now
        if (
            s.db_touched_at is not None
            and (now - s.db_touched_at).total_seconds() < _TOUCH_INTERVAL_S
        ):
            return
        store = self._ensure_session_store()
        if store is not None:
            store.touch(repo, pr)  # fail-soft внутри SessionStore
        s.db_touched_at = now

    def _invoke_tool(self, repo: str, pr: int, name: str, args: dict) -> str:
        """Вызов инструмента с per-вызов пересозданием make_tools.

        seen-дедуп сбрасывается на каждый вызов (повтор отдаёт результат из
        ctx.cache, а не заглушку «повтор»); сам кэш живёт всю сессию.
        После вызова добавляем шаг в session.steps для трассировки.
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        tools = {t.name: t for t in make_tools(s.ctx)}
        result = tools[name].invoke(args)
        if len(s.steps) < _MAX_SESSION_STEPS:
            # PRI-209: сервер не знает реальных затрат токенов на tool call
            # (LLM-вызовы происходят в клиенте), поэтому tokens/cost не пишем.
            s.steps.append({
                "stage": "analyze",
                "unit": args.get("path") or args.get("node_id") or args.get("symbol") or "",
                "seq": len(s.steps),
                "kind": "tool_call",
                "name": name,
                "text": result[:500] if isinstance(result, str) else None,
                "tool_calls": [{"name": name, "args": args}],
            })
        return result

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

    def search_tasks(self, query: str, top_k: int | None = None,
                     project: str | None = None) -> str:
        """Похожие по смыслу задачи (гибрид-поиск). При project — скоуп по проекту."""
        return self.components.task_service.search_tasks(query, top_k, project=project)

    def get_task_context(self, key: str, project: str | None = None) -> str:
        """Граф-контекст задачи. При project — соседи только этого проекта."""
        return self.components.task_service.get_task_context(key, project=project)

    def get_task(self, key: str, project: str | None = None) -> dict | None:
        """Нормализованный TaskBrief из стора. При project — только из этого проекта."""
        return self.components.task_service.get_task(key, project=project)

    def count_tasks(self, project: str | None = None) -> dict:
        """Число :Task проекта из графа (best-effort, read-only). Возвращает {"count": int}.

        Источник размера доски для рекомендации context_limits в configure-review;
        граф недоступен/сбой → {"count": 0}, вызывающий фолбэкает на вопрос."""
        return {"count": self.components.task_service.count_tasks(project)}

    def board_config(self) -> dict:
        """Глобальный (env) конфиг доски задач деплоя — для клиентских скилов.

        Скилы sync-tasks/solve-task сначала читают per-repo .review.yml; если
        там нет блока task_board, берут этот current non-secret deploy-wide fallback.
        Его тип выводится из configured registry credentials, а common non-secret metadata
        дополняет конфиг. ``{"task_board": None}`` = доска в деплое не настроена:
        выполните ``reviewer init``, задайте registry-declared provider credentials и
        проверьте подключение через ``reviewer check``.
        """
        return {"task_board": self.settings.task_board_default()}

    # --- Канал репорта багов самого reviewer (PRI-239) -------------------------

    def report_bug(
        self,
        kind: str,
        summary: str,
        *,
        expected: str = "",
        actual: str = "",
        steps: list[str] | None = None,
        severity: str | None = None,
        tool: str = "",
        skill_step: str = "",
        error_class: str = "",
        details: str = "",
        caused_by_skill_instruction: bool = False,
        repo: str = "",
        branch: str = "",
        client_environment: Mapping[str, object] | None = None,
        scale: Mapping[str, object] | None = None,
        index_drift: int | None = None,
        environment_include: list[str] | None = None,
        environment_exclude: list[str] | None = None,
        confirmed: bool = False,
        non_interactive: bool = False,
        decline: bool = False,
    ) -> dict:
        """Собрать анонимизированный репорт дефекта reviewer; опубликовать — только по апруву.

        Двухфазность — серверный инвариант, а не договорённость с промптом: без
        ``confirmed`` тул возвращает ПОЛНЫЙ итоговый текст (``status="preview"``) и
        ничего никуда не отправляет. Скилл — это промпт, а не ``try/finally``, поэтому
        гарантию «сами не ходим» даёт сервер: в неинтерактивном режиме публикация
        подменяется фолбэком даже при ``confirmed=True``.

        Исходы: ``disabled`` (канал выключен), ``not_reported`` (чужая проблема),
        ``suppressed`` (симптом уже предлагали или пользователь отказался),
        ``declined`` (зафиксирован отказ), ``preview``, ``published``, ``commented``,
        ``fallback``.
        """
        from reviewer.bugreport.environment import collect_environment
        from reviewer.bugreport.publish import publish_report
        from reviewer.bugreport.render import TARGET_REPO, render_issue
        from reviewer.bugreport.triage import triage

        verdict = triage(
            kind,
            severity=severity,
            tool=tool,
            step=skill_step,
            error_class=error_class,
            caused_by_skill_instruction=caused_by_skill_instruction,
        )
        if decline:
            # Отказ запоминается до конца жизни процесса: повторно не спрашиваем.
            self._bug_declined.add(verdict.signature)
            return {"status": "declined", "signature": verdict.signature,
                    "triage": verdict.as_dict()}

        enabled, disabled_reason = self._bug_reports_enabled(repo, branch)
        if not enabled:
            return {"status": "disabled", "reason": disabled_reason,
                    "triage": verdict.as_dict()}
        if not verdict.ours:
            return {"status": "not_reported", "reason": verdict.reason,
                    "triage": verdict.as_dict()}
        if verdict.signature in self._bug_declined:
            return {"status": "suppressed", "reason": "пользователь уже отказался от репорта "
                                                      "по этому симптому в этой сессии",
                    "signature": verdict.signature, "triage": verdict.as_dict()}
        if not confirmed and verdict.signature in self._bug_offered:
            return {"status": "suppressed", "reason": "репорт по этому симптому уже предлагался "
                                                      "в этой сессии",
                    "signature": verdict.signature, "triage": verdict.as_dict()}

        sanitizer = self._bug_sanitizer(repo, branch)
        environment = collect_environment(
            client_fields={**dict(client_environment or {}),
                           "tool": tool, "skill_step": skill_step},
            board_type=self._bug_board_type(),
            vcs_type=self.settings.vcs_provider,
            vcs_self_hosted=self._bug_vcs_self_hosted(),
            graph_backend=str(self.settings.graph_backend),
            index_drift=index_drift,
            scale=scale,
            sanitizer=sanitizer,
        ).trimmed(include=environment_include, exclude=environment_exclude or ())

        report = render_issue(
            summary=sanitizer.text(summary),
            expected=sanitizer.text(expected),
            actual=sanitizer.text(actual),
            steps=sanitizer.lines(steps or []),
            severity=verdict.severity,
            kind_reason=verdict.reason,
            signature=verdict.signature,
            environment=environment,
            details=sanitizer.text(details),
        )
        payload = {
            "signature": verdict.signature,
            "triage": verdict.as_dict(),
            "defer": verdict.defer,
            "target_repo": TARGET_REPO,
            "title": report.title,
            "issue_text": report.body,
            "environment_keys": environment.keys(),
            "identity_notice": (
                "Issue публикуется от вашего GitHub-аккаунта — ник будет виден в публичном "
                "репозитории. Альтернатива: отказаться от публикации и открыть issue вручную "
                "по ссылке-заготовке."
            ),
        }
        if not confirmed:
            self._bug_offered.add(verdict.signature)
            return {**payload, "status": "preview"}
        if non_interactive:
            # Автономных публикаций нет ни в одном режиме: «апрув» без человека
            # апрувом не является, каким бы ни был флаг confirmed.
            from reviewer.bugreport.render import prefilled_url

            return {**payload, "status": "fallback",
                    "fallback_url": prefilled_url(report),
                    "reason": "неинтерактивный режим: канал не публикует без человека"}
        result = publish_report(report, token=self.settings.github_token)
        return {**payload, **result.as_dict(), "status": result.status}

    def _bug_reports_enabled(self, repo: str, branch: str) -> tuple[bool, str]:
        """Выключатель канала: деплой (env) и репо (.review.yml) — независимо."""
        if not self.settings.review_bug_reports:
            return False, "канал выключен на уровне деплоя (REVIEW_BUG_REPORTS=false)"
        if not repo:
            return True, ""
        try:
            policy, _ = self._resolve_policy(repo, branch or self._bug_branches(repo)[0])
        except Exception:      # noqa: BLE001 — недоступная политика не включает канал молча
            log.warning("не удалось резолвить политику для канала репорта багов", exc_info=True)
            return True, ""
        if not policy.bug_reports:
            return False, "канал выключен для этого репозитория (bug_reports: false в .review.yml)"
        return True, ""

    def _bug_branches(self, repo: str) -> tuple[str, tuple[str, ...]]:
        """Отслеживаемые ветки репо через общий резолвер; сбой → безопасный дефолт.

        Прямой env-фолбэк Settings здесь запрещён guard-тестом и по делу: в мульти-репо
        деплое он дал бы ветки чужого репозитория.
        """
        from reviewer.config.branches import resolve_repo_branches

        if not repo:
            return "main", ()
        try:
            resolved = resolve_repo_branches(repo, settings=self.settings)
        except Exception:      # noqa: BLE001 — резолв веток вторичен для репорта
            return "main", ()
        return resolved.primary, tuple(resolved.index)

    def _bug_sanitizer(self, repo: str, branch: str):
        """Санитайзер, знающий литералы этой установки: репо, ветки, хосты, секреты.

        Точное знание идёт впереди эвристик: имя репозитория, упомянутое в прозе без
        слэша, регуляркой не ловится, а утечь не должно.
        """
        from reviewer.bugreport.sanitize import Sanitizer

        secrets = set(self._bug_board_secrets())
        for value in (self.settings.github_token, self.settings.gitlab_token,
                      self.settings.voyage_api_key, self.settings.neo4j_password):
            if value:
                secrets.add(value)
        repos = {value for value in (repo, self.settings.default_repo) if value}
        primary, tracked = self._bug_branches(repo)
        branches = {primary, *tracked}
        if branch:
            branches.add(branch)
        hosts = set()
        for url in (self.settings.gitlab_url, self.settings.neo4j_uri, self.settings.pg_dsn):
            host = _hostname(url)
            if host:
                hosts.add(host)
        return Sanitizer.build(secrets=secrets, repos=repos, branches=branches, hosts=hosts)

    def _bug_board_secrets(self) -> frozenset[str]:
        try:
            return self._board_secrets()
        except Exception:      # noqa: BLE001 — отсутствие доски не мешает репорту
            return frozenset()

    def _bug_board_type(self) -> str:
        """Тип зарегистрированной доски — безопасен; её project и URL в блок не идут."""
        default = self.settings.task_board_default() or {}
        return str(default.get("type") or "")

    def _bug_vcs_self_hosted(self) -> bool | None:
        """Факт self-hosted инсталляции без хоста и URL."""
        if self.settings.vcs_provider == "gitlab":
            return not self.settings.gitlab_url.startswith("https://gitlab.com")
        return False

    def purge_orphaned_tasks(
        self, active_keys: list[str], keep_with_prs: bool = True
    ) -> dict:
        """Удалить осиротевшие задачи из store и графа."""
        return self.components.task_service.purge_orphaned_tasks(
            active_keys, keep_with_prs=keep_with_prs
        )

    def _board_runtime(self) -> tuple[BoardProviderRegistry, ProviderCredentialSource]:
        registry = getattr(self, "_board_registry", None) or default_board_registry()
        credentials = getattr(self, "_board_credentials", None)
        if credentials is None:
            credentials = ProviderCredentialSource.from_settings(self.settings)
        return registry, credentials

    def _board_secrets(self) -> frozenset[str]:
        registry, credentials = self._board_runtime()
        return credentials.secret_values(
            tuple(registry.get(type_) for type_ in registry.registered_types())
        )

    @staticmethod
    def _safe_board_payload(payload: dict, secrets: frozenset[str]) -> dict:
        sanitized = sanitize_provider_payload(payload, secrets)
        return sanitized if isinstance(sanitized, dict) else {}

    @staticmethod
    def _unknown_subtask_outcome(
        request: SubtaskRequest,
        board_type: str,
        warning: str = _SUBTASK_RECOVERY_WARNING,
    ) -> dict:
        return {
            "status": "partial",
            "board_type": board_type,
            "parent_key": request.parent_key,
            "idempotency_key": request.idempotency_key,
            "resumed": False,
            "created": [],
            "attached": [],
            "unattached": [],
            "pending": [],
            "warnings": [warning],
            "reindexed": False,
            "category": "unknown_outcome",
            "retryable": True,
        }

    def _recover_subtask_failure(
        self,
        request: SubtaskRequest,
        board_type: str,
        secrets: frozenset[str],
        error: Exception,
    ) -> dict:
        warning = _SUBTASK_RECOVERY_WARNING
        try:
            rendered = sanitize_provider_text(error, secrets)
            if isinstance(rendered, str) and rendered:
                warning = rendered
        except Exception:  # noqa: BLE001 - rendering must be total
            warning = _SUBTASK_RECOVERY_WARNING

        try:
            result = self.components.subtask_service.recover_result(request)
        except Exception:  # noqa: BLE001 - durable reload must be fail-safe
            result = None
            warning = _SUBTASK_RECOVERY_WARNING
        if type(result) is not SubtaskBatchResult:
            return self._unknown_subtask_outcome(request, board_type, warning)

        try:
            payload = result.payload()
            persisted_warnings = payload.get("warnings", ())
            if not isinstance(persisted_warnings, (list, tuple)) or not all(
                isinstance(item, str) for item in persisted_warnings
            ):
                persisted_warnings = ()
            payload["warnings"] = [*persisted_warnings, warning]
            safe_payload = self._safe_board_payload(payload, secrets)
            if not safe_payload:
                raise ValueError("recovery payload sanitizer returned no payload")
            return safe_payload
        except Exception:  # noqa: BLE001 - final rendering fallback is literal-only
            return {
                "status": result.status,
                "board_type": result.board_type,
                "parent_key": result.parent_key,
                "idempotency_key": result.idempotency_key,
                "resumed": result.resumed,
                "created": [],
                "attached": [],
                "unattached": [],
                "pending": [],
                "warnings": [_SUBTASK_RECOVERY_WARNING],
                "reindexed": result.reindexed,
                "category": result.category,
                "retryable": result.retryable,
            }

    @staticmethod
    def _board_error(operation: str, error: Exception,
                     secrets: frozenset[str] = frozenset()) -> dict:
        reason = sanitize_provider_text(error, secrets)
        log.warning("%s: board operation failed: %s", operation, reason)
        result: dict[str, object] = {"status": "error", "reason": reason}
        if isinstance(error, BoardProviderError):
            result.update(
                {
                    "category": error.category,
                    "hint": sanitize_provider_text(error.hint, secrets),
                    "retryable": error.retryable,
                }
            )
        return result

    def _write_through(self, provider: TaskBoardProvider, key: str | None) -> dict | None:
        """Best-effort fetch → normalize → index после успешной board write.

        Возвращает нормализованный бриф (нужен вызывающему как источник url
        задачи для обратного линка в PR) или None, если реиндекс не удался."""
        if not key:
            log.warning("board write-through пропущен: ключ задачи не определён")
            return None
        try:
            raw = provider.fetch_one(key)
            if raw is None:
                return None
            brief = provider.normalize(raw)
            self.components.task_service.index_task(brief)
            return brief
        except Exception:
            log.warning("board write-through реиндекс не удался")
            return None

    def _write_through_subtasks(
        self,
        provider: TaskBoardProvider,
        parent: RawTask,
        identities: tuple[NativeSubtaskIdentity, ...],
        sanitize: Callable[[object], str],
    ) -> WriteThroughResult:
        """Строго переиндексировать parent и подтверждённых children одним батчем."""
        warnings: list[str] = []

        def warning(value: object) -> None:
            safe = sanitize(value)
            warnings.append(safe if isinstance(safe, str) else "write-through failed")

        try:
            if (
                not isinstance(parent, RawTask)
                or not isinstance(parent.board_id, str)
                or not parent.board_id.strip()
                or not isinstance(parent.key, str)
                or not parent.key.strip()
            ):
                warning("write-through parent snapshot has no usable identity")
                return WriteThroughResult(False, tuple(warnings))

            current_parent = provider.fetch_one(parent.board_id)
            if current_parent is None:
                warning(f"write-through parent not found: {parent.key}")
                return WriteThroughResult(False, tuple(warnings))
            if (
                not isinstance(current_parent, RawTask)
                or current_parent.board_id != parent.board_id
                or current_parent.key != parent.key
                or type(current_parent.subtask_ids) is not list
                or not all(
                    isinstance(subtask_id, str) and subtask_id.strip()
                    for subtask_id in current_parent.subtask_ids
                )
            ):
                warning("write-through parent point-read returned a different or malformed task")
                return WriteThroughResult(False, tuple(warnings))

            try:
                validate_confirmed_subtask_identities(
                    current_parent.board_id,
                    tuple(enumerate(identities)),
                )
            except ConfirmedSubtaskIdentityError as error:
                warning(error)
                return WriteThroughResult(False, tuple(warnings))

            confirmed_ids = {identity.board_id for identity in identities}
            if not confirmed_ids.issubset(current_parent.subtask_ids):
                warning("write-through parent no longer contains all confirmed subtasks")
                return WriteThroughResult(False, tuple(warnings))

            raw_tasks = [current_parent]
            for identity in identities:
                raw = provider.fetch_one(identity.board_id)
                if raw is None:
                    warning(f"write-through task not found: {identity.key}")
                    return WriteThroughResult(False, tuple(warnings))
                if not isinstance(raw, RawTask) or raw.board_id != identity.board_id:
                    warning("write-through child point-read returned a different task")
                    return WriteThroughResult(False, tuple(warnings))
                canonical_keys = {
                    value
                    for value in (raw.key, raw.project_code)
                    if isinstance(value, str) and value.strip()
                }
                if identity.key != identity.board_id and identity.key not in canonical_keys:
                    warning("write-through child canonical identity does not match point-read")
                    return WriteThroughResult(False, tuple(warnings))
                raw_tasks.append(raw)

            briefs: list[dict] = []
            seen_keys: set[str] = set()
            for raw in raw_tasks:
                brief = provider.normalize(raw)
                if (
                    not isinstance(brief, dict)
                    or not isinstance(brief.get("key"), str)
                    or not brief["key"].strip()
                    or type(brief.get("links")) is not list
                    or not all(
                        isinstance(link, dict)
                        and isinstance(link.get("type"), str)
                        and link["type"].strip()
                        and isinstance(link.get("key"), str)
                        and link["key"].strip()
                        for link in brief["links"]
                    )
                ):
                    warning("write-through normalize returned an incomplete TaskBrief")
                    return WriteThroughResult(False, tuple(warnings))
                key = brief["key"]
                if key in seen_keys:
                    warning(f"write-through duplicate normalized task key: {key}")
                    return WriteThroughResult(False, tuple(warnings))
                seen_keys.add(key)
                briefs.append(brief)

            child_keys = {brief["key"] for brief in briefs[1:]}
            parent_subtask_keys = {
                link["key"]
                for link in briefs[0]["links"]
                if link["type"] == "subtask"
            }
            if not child_keys.issubset(parent_subtask_keys):
                warning("write-through parent links do not cover normalized subtasks")
                return WriteThroughResult(False, tuple(warnings))

            results = self.components.task_service.index_batch(briefs)
            if not isinstance(results, list) or len(results) != len(briefs):
                warning("write-through index result count does not match TaskBrief count")
                return WriteThroughResult(False, tuple(warnings))

            for brief, result in zip(briefs, results):
                if not isinstance(result, dict) or result.get("key") != brief["key"]:
                    warning("write-through index result does not match TaskBrief")
                    continue
                result_warnings = result.get("warnings")
                if type(result_warnings) is not list or not all(
                    isinstance(item, str) for item in result_warnings
                ):
                    warning("write-through index returned invalid warnings")
                else:
                    for item in result_warnings:
                        warning(item)
                if result.get("links_stored") is not True:
                    warning(f"write-through links were not stored for {brief['key']}")
        except Exception as error:  # noqa: BLE001 - provider/store boundary
            warning(error)
            return WriteThroughResult(False, tuple(warnings))
        return WriteThroughResult(not warnings, tuple(warnings))

    def _resolved_subtask_request(
        self,
        parent_key: str,
        subtasks: list[dict],
        idempotency_key: str,
        board_type: str | None,
        project: str | None,
        provider_options: Mapping[str, JsonValue] | None,
    ):
        """Резолв generic board config без создания provider и нормализация hash input."""
        registry, credentials = self._board_runtime()
        durable_request = None
        if board_type is None or project is None or provider_options is None:
            durable_request = self.components.subtask_service.lookup_request(idempotency_key)
        defaults = (
            self.settings.task_board_default() or {}
            if durable_request is None
            else {}
        )

        requested_type = (
            board_type
            if board_type is not None
            else durable_request.board_type
            if durable_request is not None
            else None
        )
        if requested_type is None:
            default_type = defaults.get("type") if isinstance(defaults, dict) else None
            if isinstance(default_type, str):
                requested_type = default_type
        if requested_type is not None:
            if not isinstance(requested_type, str) or not requested_type.strip():
                raise BoardProviderError("configuration", "board_type must be a non-empty string.")
            resolved_type: str | None = requested_type.strip()
        else:
            configured = registry.configured_types(credentials)
            resolved_type = configured[0] if len(configured) == 1 else None
        if resolved_type is None:
            raise BoardProviderError(
                "configuration",
                "board_type is required unless exactly one provider is configured.",
            )
        try:
            registry.get(resolved_type)
        except KeyError:
            raise BoardProviderError(
                "configuration", "Board provider type is not registered."
            ) from None
        default_project = defaults.get("project") if isinstance(defaults, dict) else None
        resolved_project = (
            project
            if project is not None
            else durable_request.project
            if durable_request is not None
            else default_project
        )
        default_options = defaults.get("options", {}) if isinstance(defaults, dict) else {}
        if not isinstance(default_options, Mapping):
            raise BoardProviderError("configuration", "Board provider options are invalid.")
        if provider_options is not None and not isinstance(provider_options, Mapping):
            raise BoardProviderError("configuration", "Board provider options are invalid.")
        if provider_options is not None:
            options = (
                dict(provider_options)
                if durable_request is not None
                else {**default_options, **dict(provider_options)}
            )
        elif durable_request is not None:
            options = durable_request.provider_options
        else:
            options = dict(default_options)
        request = validate_subtask_request(
            parent_key,
            subtasks,
            idempotency_key,
            resolved_type,
            resolved_project,
            options,
        )
        return request, registry, credentials

    def _backlink_pr(self, pr_url: str, key: str, task_url: str) -> tuple[str, list[str]]:
        """Дописать ссылку на задачу в начало тела PR. Возвращает (status, warnings).

        status — один из трёх исходов: "added" (записали сейчас), "already_present"
        (ссылка уже была в теле — идемпотентный no-op, это норма) и "failed"
        (записать не удалось; причина — в warnings). Один bool их не различал, и
        клиент рапортовал о проблеме, которой не было (PRI-238).

        Обратная сторона связи: finish пишет PR-ссылку в задачу, здесь — ссылку
        на задачу в PR. Полностью fail-soft: доска к этому моменту уже записана,
        поэтому ни одна ошибка правки PR не отменяет успех finish_task."""
        from reviewer.tasks.pr_backlink import apply_backlink, parse_pr_url
        if not task_url:
            return "failed", ["ссылка на задачу не добавлена в PR: url задачи не резолвится "
                              "(task_board.url_template не задан)"]
        target = parse_pr_url(pr_url)
        if target is None:
            return "failed", ["ссылка на задачу не добавлена в PR: "
                              f"{pr_url!r} не распознан как ссылка на PR/MR"]
        vcs = None
        try:
            vcs = (self._vcs_factory(target.owner, target.repo) if self._vcs_factory
                   else self._review_service._create_vcs_provider(
                       target.owner, target.repo,
                       platform=target.platform, base_url=target.base_url))
            body = apply_backlink(vcs.get_pull_request(target.number).body, key, task_url)
            if body is None:
                return "already_present", []   # ссылка уже на месте — идемпотентный no-op
            vcs.update_pull_request_body(target.number, body)
            return "added", []
        except Exception as exc:
            log.warning("бэклинк задачи в PR не удался", exc_info=True)
            return "failed", [f"ссылка на задачу не добавлена в PR: {exc}"]
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("не удалось закрыть VCS после бэклинка", exc_info=True)

    def sync_board(
        self,
        board: str | None = None,
        limit: int | None = None,
        purge_orphaned: bool = False,
        keep_with_prs: bool = True,
        board_type: str | None = None,
        provider_options: Mapping[str, JsonValue] | None = None,
        force_renormalize: bool = False,
        *,
        repo: str | None = None,
        branch: str | None = None,
        sync_filter: Mapping[str, JsonValue] | None = None,
        status_field: str | None = None,
    ) -> dict:
        """Server-side ETL в explicit-режиме или из effective policy репозитория."""
        if repo is None and branch is not None:
            return self._board_error(
                "sync_board",
                BoardProviderError(
                    "configuration",
                    "branch requires repo in sync_board.",
                ),
            )

        if repo is not None:
            if not isinstance(repo, str) or not repo.strip():
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        "repo must be a non-empty owner/name value.",
                    ),
                )
            mixed = [
                name
                for name, supplied in (
                    ("board", board is not None),
                    ("board_type", board_type is not None),
                    ("provider_options", provider_options is not None),
                    ("sync_filter", sync_filter is not None),
                    ("status_field", status_field is not None),
                )
                if supplied
            ]
            if mixed:
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        "repo mode cannot be combined with explicit board arguments: "
                        + ", ".join(mixed),
                    ),
                )

            repo_secrets = self._board_secrets() | frozenset(
                secret
                for secret in (
                    self.settings.github_token,
                    self.settings.gitlab_token,
                )
                if secret
            )
            resolved_repo_branch = self._resolve_repo_branch(repo, branch)
            if isinstance(resolved_repo_branch, str):
                reason = resolved_repo_branch.removeprefix("(").removesuffix(")")
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        reason,
                        secrets=repo_secrets,
                    ),
                    repo_secrets,
                )
            normalized_repo, resolved_branch = resolved_repo_branch
            try:
                policy, meta = self._resolve_policy(normalized_repo, resolved_branch)
            except Exception as error:
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        "Task board policy could not be resolved: "
                        f"{type(error).__name__}: {error}",
                        secrets=repo_secrets,
                    ),
                    repo_secrets,
                )
            from reviewer.config.layers import COMMITTED_UNREAD_WARNING_PREFIX

            blocking_warnings = [
                warning
                for warning in meta.warnings
                if not warning.startswith(COMMITTED_UNREAD_WARNING_PREFIX)
            ]
            if blocking_warnings:
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        "Task board policy resolution warnings: "
                        + "; ".join(blocking_warnings),
                        secrets=repo_secrets,
                    ),
                    repo_secrets,
                )
            task_board = policy.task_board
            if task_board is None:
                return {
                    "status": "error",
                    "reason": "task board REST is not configured",
                }
            if not isinstance(task_board, Mapping):
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        "Effective task_board policy must be a mapping.",
                    ),
                )
            repo_board_type = task_board.get("type")
            if not isinstance(repo_board_type, str) or not repo_board_type.strip():
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        "Effective task_board.type must be an explicit non-empty string.",
                        secrets=repo_secrets,
                    ),
                    repo_secrets,
                )
            options = task_board.get("options")
            if options is not None and not isinstance(options, Mapping):
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        "Effective task_board.options must be a mapping.",
                    ),
                )
            try:
                typed_filter = (
                    normalize_task_sync_filter(task_board["sync_filter"])
                    if "sync_filter" in task_board
                    else None
                )
            except (TypeError, ValueError) as error:
                return self._board_error(
                    "sync_board",
                    BoardProviderError(
                        "configuration",
                        str(error),
                        secrets=repo_secrets,
                    ),
                    repo_secrets,
                )

            registry, credentials = self._board_runtime()
            try:
                with resolved_provider(
                    self.settings,
                    repo_board_type,
                    options,
                    registry=registry,
                    credential_source=credentials,
                ) as resolved:
                    scoped = SyncService(
                        [SyncProvider(resolved.provider, resolved.secrets)],
                        self.components.task_service,
                        self.components.store,
                    )
                    result = scoped.run(
                        board=task_board.get("project"),
                        limit=limit,
                        purge_orphaned=purge_orphaned,
                        keep_with_prs=keep_with_prs,
                        board_type=resolved.board_type,
                        force_renormalize=force_renormalize,
                        sync_filter=typed_filter,
                        filter_source=meta.sources.get("task_board", "env"),
                    )
                    result.setdefault("warnings", []).extend(
                        policy.task_board_warnings
                    )
                    return self._safe_board_payload(result, resolved.secrets)
            except BoardProviderError as error:
                return self._board_error("sync_board", error)

        try:
            typed_filter = (
                normalize_task_sync_filter(sync_filter)
                if sync_filter is not None
                else None
            )
        except (TypeError, ValueError) as error:
            return self._board_error(
                "sync_board",
                BoardProviderError("configuration", str(error)),
            )
        filter_source = "explicit" if sync_filter is not None else None
        _, options, migration_warnings = migrate_legacy_board_args(
            target=None,
            provider_options=provider_options,
            status_field=status_field,
        )
        sync = getattr(self.components, "sync_service", None)
        if board_type is None and not options:
            if sync is None:
                return {
                    "status": "error",
                    "reason": "task board REST is not configured",
                }
            secrets = self._board_secrets()
            try:
                result = sync.run(
                    board=board,
                    limit=limit,
                    purge_orphaned=purge_orphaned,
                    keep_with_prs=keep_with_prs,
                    force_renormalize=force_renormalize,
                    sync_filter=typed_filter,
                    filter_source=filter_source,
                )
            except Exception as error:
                return self._board_error("sync_board", error, secrets)
            result.setdefault("warnings", []).extend(migration_warnings)
            return self._safe_board_payload(result, secrets)

        registry, credentials = self._board_runtime()
        try:
            with resolved_provider(
                self.settings,
                board_type,
                options,
                registry=registry,
                credential_source=credentials,
            ) as resolved:
                scoped = SyncService(
                    [SyncProvider(resolved.provider, resolved.secrets)],
                    self.components.task_service,
                    self.components.store,
                )
                result = scoped.run(
                    board=board,
                    limit=limit,
                    purge_orphaned=purge_orphaned,
                    keep_with_prs=keep_with_prs,
                    board_type=resolved.board_type,
                    force_renormalize=force_renormalize,
                    sync_filter=typed_filter,
                    filter_source=filter_source,
                )
                result.setdefault("warnings", []).extend(migration_warnings)
                return self._safe_board_payload(result, resolved.secrets)
        except BoardProviderError as error:
            return self._board_error("sync_board", error)

    def finish_task(
        self,
        key: str,
        pr_url: str,
        note: str | None = None,
        mark_done: bool = True,
        board_type: str | None = None,
        target: str | None = None,
        provider_options: Mapping[str, JsonValue] | None = None,
        *,
        done_state: str | None = None,
        status_field: str | None = None,
        done_column: str | None = None,
    ) -> dict:
        """Закрыть задачу через generic target/options и выполнить write-through."""
        target, options, migration_warnings = migrate_legacy_board_args(
            target=target,
            provider_options=provider_options,
            done_state=done_state,
            status_field=status_field,
            done_column=done_column,
        )
        registry, credentials = self._board_runtime()
        try:
            with resolved_provider(
                self.settings,
                board_type,
                options,
                registry=registry,
                credential_source=credentials,
            ) as resolved:
                result = resolved.provider.finish(
                    key,
                    pr_url,
                    note=note,
                    mark_done=mark_done,
                    target=target,
                )
                brief = self._write_through(resolved.provider, key)
                link_status, link_warnings = self._backlink_pr(
                    pr_url, key, (brief or {}).get("url") or "")
                result.setdefault("warnings", []).extend(migration_warnings)
                result["warnings"].extend(link_warnings)
                return self._safe_board_payload(
                    {
                        "status": "ok",
                        "board_type": resolved.board_type,
                        "reindexed": brief is not None,
                        # task_link_added — прежняя семантика («записали сейчас»),
                        # task_link_status — три различимых исхода (PRI-238).
                        "task_link_added": link_status == "added",
                        "task_link_status": link_status,
                        **result,
                    },
                    resolved.secrets,
                )
        except BoardProviderError as error:
            return self._board_error("finish_task", error)

    def create_task(
        self,
        title: str,
        problem: str = "",
        steps: list[str] | None = None,
        criteria: list[str] | None = None,
        context: str | None = None,
        board_type: str | None = None,
        project: str | None = None,
        target: str | None = None,
        provider_options: Mapping[str, JsonValue] | None = None,
        *,
        status_field: str | None = None,
    ) -> dict:
        """Создать задачу через generic target/options и выполнить write-through."""
        target, options, migration_warnings = migrate_legacy_board_args(
            target=target,
            provider_options=provider_options,
            status_field=status_field,
        )
        doc_md = render_markdown(
            TaskDoc(
                title=title,
                problem=problem,
                steps=list(steps or []),
                criteria=list(criteria or []),
                context=context,
            )
        )
        registry, credentials = self._board_runtime()
        try:
            with resolved_provider(
                self.settings,
                board_type,
                options,
                registry=registry,
                credential_source=credentials,
            ) as resolved:
                result = resolved.provider.create(
                    doc_md,
                    title=title,
                    target=target,
                    project=project,
                )
                reindexed = self._write_through(resolved.provider, result.get("key")) is not None
                result.setdefault("warnings", []).extend(migration_warnings)
                return self._safe_board_payload(
                    {
                        "status": "ok",
                        "board_type": resolved.board_type,
                        "reindexed": reindexed,
                        **result,
                    },
                    resolved.secrets,
                )
        except BoardProviderError as error:
            return self._board_error("create_task", error)

    def create_subtasks(
        self,
        parent_key: str,
        subtasks: list[dict],
        idempotency_key: str,
        board_type: str | None = None,
        project: str | None = None,
        provider_options: Mapping[str, JsonValue] | None = None,
    ) -> dict:
        """Создать и привязать batch нативных подзадач с durable idempotency."""
        secrets = frozenset()
        try:
            secrets = self._board_secrets()
            request, registry, credentials = self._resolved_subtask_request(
                parent_key,
                subtasks,
                idempotency_key,
                board_type,
                project,
                provider_options,
            )
            secrets = credentials.secret_values(registry.get(request.board_type))
            preflight = self.components.subtask_service.preflight(request)
            if preflight.result is not None:
                return self._safe_board_payload(preflight.result.payload(), secrets)

            with resolved_provider(
                self.settings,
                request.board_type,
                request.provider_options,
                registry=registry,
                credential_source=credentials,
            ) as resolved:
                if "native_subtasks" not in resolved.capabilities:
                    return self._safe_board_payload(
                        {
                            "status": "error",
                            "board_type": resolved.board_type,
                            "parent_key": request.parent_key,
                            "idempotency_key": request.idempotency_key,
                            "category": "unsupported",
                            "retryable": False,
                            "reason": "Board provider does not support native subtasks.",
                        },
                        resolved.secrets,
                    )
                def sanitize(value: object) -> str:
                    return sanitize_provider_text(value, resolved.secrets)

                try:
                    result = self.components.subtask_service.run(
                        request,
                        operation=preflight.operation,
                        provider=resolved.provider,
                        board_type=resolved.board_type,
                        write_through=lambda parent, identities: self._write_through_subtasks(
                            resolved.provider,
                            parent,
                            identities,
                            sanitize,
                        ),
                        sanitize=sanitize,
                    )
                except BoardProviderError:
                    raise
                except Exception as error:  # noqa: BLE001 - durable recovery boundary
                    return self._recover_subtask_failure(
                        request,
                        request.board_type or resolved.board_type,
                        resolved.secrets,
                        error,
                    )
                return self._safe_board_payload(result.payload(), resolved.secrets)
        except BoardProviderError as error:
            return self._safe_board_payload(
                self._board_error("create_subtasks", error, secrets),
                secrets,
            )
        except Exception as error:  # noqa: BLE001 - ledger/config boundary
            return self._safe_board_payload(
                self._board_error("create_subtasks", error, secrets),
                secrets,
            )

    def get_board_targets(
        self,
        board_type: str | None = None,
        project: str | None = None,
        provider_options: Mapping[str, JsonValue] | None = None,
    ) -> dict:
        """Нормализованные targets/options зарегистрированной доски."""
        registry, credentials = self._board_runtime()
        try:
            with resolved_provider(
                self.settings,
                board_type,
                provider_options,
                registry=registry,
                credential_source=credentials,
            ) as resolved:
                result = resolved.provider.list_targets(project)
                return self._safe_board_payload(
                    {
                        **result,
                        "board_type": resolved.board_type,
                        "project": project,
                        "capabilities": sorted(resolved.capabilities),
                    },
                    resolved.secrets,
                )
        except BoardProviderError as error:
            return self._board_error("get_board_targets", error)

    def _resolve_repo_branch(self, repo: str, branch: str | None) -> tuple[str, str] | str:
        """Резолв (repo, ветка) для session-less тулов.

        Возвращает (normalized_repo, resolved_branch) при успехе либо строку-заметку
        об ошибке (её тул отдаёт пользователю как есть). Ветка валидируется по
        отслеживаемым веткам репозитория (см. `reviewer config show`); пустая
        ветка → первичная ветка репозитория.
        """
        from reviewer.services.repo_id import normalize_repo
        raw = repo or self.settings.default_repo
        if not raw:
            return "(repo не задан: передайте repo или задайте DEFAULT_REPO)"
        try:
            repo = normalize_repo(raw)
        except ValueError:
            return f"(некорректный repo: {raw!r})"

        from reviewer.config.branches import resolve_repo_branches
        from reviewer.services.branch import resolve_branch

        try:
            branches = resolve_repo_branches(repo, settings=self.settings)
            return (repo, resolve_branch(branch, None, branches))
        except ValueError as exc:
            return f"({exc})"

    def _repo_clone_path(self, repo: str) -> str | None:
        """Путь к локальному клону репо из индекса, если он там записан (PRI-235).

        Пишет его `reviewer index`, который и так выполняется из клона. Fail-soft:
        недоступный Postgres или индекс, построенный до миграции, просто лишают
        резолв локального пути — коммиченный слой уйдёт читаться через VCS.
        Годность самого пути проверяет CommittedLayerFetcher: MCP-сервер может
        работать не на той машине, где индексировали.
        """
        store = getattr(self.components, "store", None)
        if store is None:
            return None
        try:
            return store.get_repo_clone(repo)
        except Exception:  # noqa: BLE001 — путь вторичен, VCS остаётся фолбэком
            log.warning("Не удалось прочитать путь к клону для %s", repo)
            return None

    def _resolve_policy(self, repo: str, branch: str) -> tuple["ReviewPolicy", "ResolutionMeta"]:
        """Резолвит effective policy из env, home-слоёв и committed `.review.yml`."""
        from reviewer.config.committed import CommittedLayerFetcher
        from reviewer.config.layers import resolve_policy_data
        from reviewer.policy.policy import ReviewPolicy
        owner, name = repo.split("/", 1)
        vcs = None

        def vcs_fetch_factory():
            # Провайдер создаётся ЛЕНИВО: при живом клоне (PRI-235) он не нужен
            # вовсе, а его создание — это ещё и токен, которого может не быть.
            nonlocal vcs
            if vcs is None:
                vcs = (
                    self._vcs_factory(owner, name)
                    if self._vcs_factory is not None
                    else self._review_service._create_vcs_provider(owner, name)
                )
            return lambda ref: vcs.get_file_at_ref(".review.yml", ref)

        try:
            fetch_committed = CommittedLayerFetcher(
                repo,
                clone_path=self._repo_clone_path(repo),
                vcs_fetch_factory=vcs_fetch_factory,
            )
            # strict_committed=False намеренно: у _resolve_policy четыре
            # потребителя, и три из них (_resolve_summary_layout,
            # _resolve_summary_topk_threshold, _resolve_context_limits) уже
            # обёрнуты в собственный fail-soft с откатом на env-дефолты.
            # Строгий режим здесь заставил бы их молча потерять
            # home:repos/<repo>.yml — то есть воспроизвёл бы исходный баг
            # PRI-234 этажом выше. Ревью остаётся громким за счёт
            # ReviewService.prepare.
            data, meta = resolve_policy_data(
                repo,
                branch,
                fetch_committed,
                strict_committed=False,
            )
            for warning in meta.warnings:
                log.warning("Слой policy пропущен: %s", warning)
            return ReviewPolicy.load_data(self.settings, data), meta
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("_resolve_policy: не удалось закрыть VCS", exc_info=True)

    def _resolve_summary_layout(
        self, repo: str, branch: str
    ) -> tuple[int, dict[str, int], list[str], str]:
        """Резолв layout-политики сводок: глубина, overrides, ignore-фильтр, источник.

        Fail-soft: при сбое резолва политики — env-глубина и ДЕФОЛТНЫЙ фильтр,
        а не пустой. Пустой фильтр при сбое молча вернул бы тестовые кластеры
        и сделал бы стоимость прохода недетерминированной.
        """
        from reviewer.policy.policy import DEFAULT_SUMMARY_PATHS_IGNORE

        default = self.settings.summary_cluster_depth
        try:
            policy, meta = self._resolve_policy(repo, branch)
            source = meta.sources.get(
                "summary_cluster_depth",
                meta.sources.get("summary_cluster_depth_overrides", "env"),
            )
            return (
                policy.summary_cluster_depth,
                policy.summary_cluster_depth_overrides,
                list(policy.summary_paths_ignore),
                source,
            )
        except Exception:
            log.warning("_resolve_summary_layout: fail-soft → env-дефолт")
            return default, {}, list(DEFAULT_SUMMARY_PATHS_IGNORE), "env"

    def _resolve_summary_topk_threshold(self, repo: str, branch: str) -> tuple[int, str]:
        """Резолв порога масштаба приора сводок с сохранением env-дефолта."""
        default = self.settings.summary_topk_threshold
        try:
            policy, meta = self._resolve_policy(repo, branch)
            source = meta.sources.get("summary_topk_threshold", "env")
            return policy.summary_topk_threshold, source
        except Exception:
            log.warning("_resolve_summary_topk_threshold: fail-soft → env-дефолт")
            return default, "env"

    def _resolve_context_limits(self, repo: str, branch: str) -> "ContextLimits":
        """Лимиты контекста из всех policy-слоёв. Fail-soft → дефолт-константы."""
        from reviewer.policy.context_limits import ContextLimits
        try:
            policy, _ = self._resolve_policy(repo, branch)
            return policy.context_limits
        except Exception:
            log.warning("_resolve_context_limits: fail-soft → дефолт-константы")
            return ContextLimits()

    def search_codebase(self, repo: str, query: str, top_k: int | None = None,
                        branch: str | None = None,
                        include_tests: bool = False) -> str:
        """Гибрид-поиск по base-индексу репозитория (без PR-сессии) — для /solve-task.

        branch — отслеживаемая ветка репозитория (см. `reviewer config show`);
        по умолчанию первичная. Поиск идёт по индексу указанной ветки (base:<branch>).
        Выдача: без вложенных дублей и (по умолчанию) без тест-чанков, с
        построчными номерами для цитирования path:line без повторного Read.
        include_tests=True возвращает тест-чанки. Охват адаптивен (cliff-отсечка
        реранкера, PRI-202): top_k — необязательный override потолка (ceiling)
        для этого вызова; None → потолок берётся из effective layered policy
        либо дефолта.
        """
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        cl = self._resolve_context_limits(repo, resolved)
        try:
            pack = self.components.retriever.search_base(
                repo, query, limits=cl.search_codebase, hops=cl.graph.hops,
                ceiling_override=top_k, branch=resolved, include_tests=include_tests)
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
        cl = self._resolve_context_limits(repo, resolved)
        try:
            neighbors = self.components.graph.expand_detailed(
                repo, [node_id], hops=2, branch=resolved)
        except Exception:
            log.warning("related_symbols: сбой графа", exc_info=True)
            return "(нет связей)"
        return format_neighbors(
            neighbors, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(нет связей)",
            cap=cl.graph.callers_topk)

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
        cl = self._resolve_context_limits(repo, resolved)
        try:
            found = self.components.graph.callers_detailed(
                repo, [node_id], branch=resolved)
        except Exception:
            log.warning("callers: сбой графа", exc_info=True)
            return "(вызовов не найдено)"
        return format_neighbors(
            found, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(вызовов не найдено)",
            cap=cl.graph.callers_topk)

    def implementations(self, repo: str, node_id: str,
                        branch: str | None = None) -> str:
        """Кто реализует/наследует символ node_id ('path#fqn') — входящие
        IMPLEMENTS, без PR-сессии. Класс → подклассы; метод → override-ы.
        На элемент: file:line + строка определения + [IMPLEMENTS].
        Точны после полного `reviewer index` с SCIP."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        if self.components.graph is None:
            return "(граф недоступен)"
        cl = self._resolve_context_limits(repo, resolved)
        try:
            found = self.components.graph.implementations_detailed(
                repo, [node_id], branch=resolved)
        except Exception:
            log.warning("implementations: сбой графа", exc_info=True)
            return "(implementations не найдены)"
        return format_neighbors(
            found, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(implementations не найдены)",
            cap=cl.graph.callers_topk)

    def get_file_skeletons(
        self, repo: str, paths: list[str], branch: str | None = None
    ) -> dict[str, str]:
        """AST-скелеты проиндексированных файлов пачкой (PRI-245), без PR-сессии.

        Источник — чанки base-индекса ветки, то есть ровно тот материал, из
        которого считается skeleton_hash: файловый job сводок читает то же, что
        инвалидирует его результат. Ключ ответа есть у КАЖДОГО запрошенного
        пути — отсутствие, пустой скелет и превышение капа возвращаются нотой,
        а не молчаливым пропуском.
        """
        from reviewer.index.chunker import file_skeleton_lines

        requested = [str(p) for p in (paths or []) if p]
        if not requested:
            return {}
        accepted = requested[:_MAX_SKELETON_PATHS]
        overflow = requested[_MAX_SKELETON_PATHS:]
        out = {
            path: f"(превышен лимит путей на вызов: {_MAX_SKELETON_PATHS})"
            for path in overflow
        }
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {**out, **{path: f"({rb})" for path in accepted}}
        repo, resolved = rb
        try:
            rows = self.components.store.fetch_chunks_at_paths(
                repo, resolved, accepted
            )
        except Exception:
            log.warning("get_file_skeletons: сбой чтения чанков", exc_info=True)
            return {**out, **{p: "(чтение скелета недоступно)" for p in accepted}}
        grouped: dict[str, list[tuple[int, str]]] = {}
        for path, start_line, text in rows:
            grouped.setdefault(path, []).append((start_line, text))
        for path in accepted:
            chunks = grouped.get(path)
            if not chunks:
                out[path] = f"(файл не найден в индексе: {path})"
                continue
            skeleton = file_skeleton_lines(chunks)
            if not skeleton:
                out[path] = "(нет определений для скелета)"
                continue
            capped = len(skeleton) > _MAX_SKELETON_LINES
            body = "\n".join(
                f"{n}|{text}" for n, text in skeleton[:_MAX_SKELETON_LINES]
            )
            out[path] = body + "\n(…усечено)" if capped else body
        return out

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
                repo, symbol, ceiling_override=3, branch=resolved, include_tests=True)
            return pack.as_context(line_numbers=True) or "(определение не найдено)"
        except Exception:
            log.warning("definition: сбой", exc_info=True)
            return "(определение не найдено)"

    def _summary_state(
        self,
        repo: str,
        branch: str,
        *,
        depth: int | None = None,
        min_size: int = 1,
        with_centrality: bool = False,
    ) -> _SummaryState:
        """Собрать единый снимок для list/work/index без расхождения вывода."""
        from reviewer.graph.summaries import (
            Member,
            build_clusters,
            canonicalize_layout,
            compute_file_fingerprints,
            compute_layout_token,
        )
        from reviewer.index.pathfilter import is_ignored

        raw = self.components.store.list_base_members(repo, branch)
        if not raw:
            resolved_depth, _, ignore, _ = self._resolve_summary_layout(repo, branch)
            if depth is not None:
                resolved_depth = depth
            return _SummaryState(
                depth=resolved_depth,
                layout_token=compute_layout_token(resolved_depth, {}, ignore),
                depth_source="env" if depth is None else "arg",
                members=[],
                clusters=[],
                file_fingerprints={},
                fragments=[],
                completed_depth=None,
                completed_layout=None,
            )
        resolved_depth, policy_overrides, ignore, policy_source = (
            self._resolve_summary_layout(repo, branch)
        )
        if depth is None:
            overrides, depth_source = policy_overrides, policy_source
        else:
            resolved_depth, overrides, depth_source = depth, {}, "arg"
        overrides, ignore, layout_token = canonicalize_layout(
            resolved_depth,
            overrides,
            ignore,
        )
        members = [
            Member(
                node_id=f"{path}#{symbol}",
                path=path,
                content_hash=content_hash,
                start_line=start_line,
                skeleton_hash=skeleton_hash,
            )
            for path, symbol, content_hash, start_line, skeleton_hash in raw
            if not is_ignored(path, ignore)
        ]
        graph = self.components.graph
        in_degree_fn = (
            (lambda ids: graph.in_degree(repo, ids, branch=branch))
            if with_centrality and graph is not None
            else None
        )
        clusters = build_clusters(
            members,
            in_degree_fn,
            depth=resolved_depth,
            min_size=min_size,
            depth_overrides=overrides,
        )
        fragments = [
            StoredSummaryFragment(
                cluster_key=item["cluster_key"],
                path=item["path"],
                fingerprint=item["fingerprint"],
                summary=item["summary"],
                provenance=item.get("provenance", {}),
            )
            for item in self.components.summary_store.get_fragments(repo, branch)
        ]
        completed_layout = self.components.summary_store.get_completed_layout(
            repo, branch
        )
        return _SummaryState(
            depth=resolved_depth,
            layout_token=layout_token,
            depth_source=depth_source,
            members=members,
            clusters=clusters,
            file_fingerprints=compute_file_fingerprints(members),
            fragments=fragments,
            completed_depth=self.components.summary_store.get_completed_depth(
                repo, branch
            ),
            completed_layout=(
                completed_layout
                if isinstance(completed_layout, str)
                else None
            ),
        )

    @staticmethod
    def _cluster_generation_flags(
        state: _SummaryState,
        cluster: "Cluster",
    ) -> tuple[bool, bool]:
        current = {
            path: state.file_fingerprints[path]
            for path in cluster.files
        }
        complete = has_complete_fragment_generation(
            cluster.key,
            current,
            state.fragments,
            state.depth,
            state.layout_token,
        )
        return state.bootstrap and not complete, state.full_rebuild and not complete

    @classmethod
    def _summary_delta(cls, state: _SummaryState, cluster: "Cluster") -> FragmentDelta:
        current = {
            path: state.file_fingerprints[path]
            for path in cluster.files
        }
        bootstrap, full_rebuild = cls._cluster_generation_flags(state, cluster)
        delta = build_fragment_delta(
            cluster.key,
            current,
            state.fragments,
            bootstrap=bootstrap,
            full_rebuild=full_rebuild,
        )
        if full_rebuild:
            return FragmentDelta(
                added=(),
                changed=delta.added + delta.changed,
                removed=delta.removed,
                moved=(),
                reused=(),
            )
        return delta

    @staticmethod
    def _fragment_ref(item: FragmentFile, *, include_content: bool = False) -> dict:
        result = {"path": item.path, "fingerprint": item.fingerprint}
        if item.from_cluster_key is not None:
            result["from_cluster_key"] = item.from_cluster_key
        if include_content:
            result["summary"] = item.summary
            result["provenance"] = dict(item.provenance)
        return result

    def _serialize_summary_delta(
        self,
        delta: FragmentDelta,
        *,
        include_reused_content: bool,
    ) -> dict:
        return {
            "added_files": [self._fragment_ref(item) for item in delta.added],
            "changed_files": [self._fragment_ref(item) for item in delta.changed],
            "removed_files": [item.path for item in delta.removed],
            "moved_files": [
                self._fragment_ref(item, include_content=include_reused_content)
                for item in delta.moved
            ],
            "reused_fragments": (
                [
                    self._fragment_ref(item, include_content=True)
                    for item in delta.reused
                ]
                if include_reused_content
                else []
            ),
        }

    @staticmethod
    def _compact_cluster_record(
        cluster: "Cluster",
        *,
        stale: bool,
        bootstrap: bool,
        full_rebuild: bool,
        delta: FragmentDelta,
    ) -> dict:
        """Запись кластера без file-level payload: только метаданные и счётчики.

        Счётчики намеренно названы ``added``/``changed``/``removed``/``moved``
        (без суффикса ``_files``): одноимённые ключи полного формата — списки, и
        совпадение имён при разных типах ломало бы клиента, не проверившего режим.
        """
        return {
            "cluster_key": cluster.key,
            "num_members": cluster.num_members,
            "source_hash": cluster.source_hash,
            "stale": stale,
            "bootstrap": bootstrap,
            "full_rebuild": full_rebuild,
            "reused_files": len(delta.reused),
            "added": len(delta.added),
            "changed": len(delta.changed),
            "removed": len(delta.removed),
            "moved": len(delta.moved),
        }

    def list_subsystem_clusters(self, repo: str, branch: str | None = None,
                                depth: int | None = None, min_size: int | None = None,
                                cap: int | None = None,
                                *,
                                compact: bool = False,
                                offset: int = 0,
                                limit: int | None = None) -> dict:
        """Кластеризовать base-граф по модулям → кластеры для /summarize-subsystems.
        cap (дефолт Settings.summary_rebuild_cap; None/0=безлимит) отбрасывает наименее
        приоритетные stale-кластеры (без сводки → старейшие updated_at первыми) и считает
        их в deferred (PRI-165). Ответ содержит layout_token — canonical identity default
        depth + normalized overrides. Если depth не задан, он берётся из effective layered
        policy с fail-soft env-дефолтом. offset/limit пагинируют финальный (после cap)
        детерминированно отсортированный по cluster_key набор кластеров.
        В полном формате ``files`` содержит только неизменённые файлы: пути из
        added_files/changed_files/moved_files в нём не повторяются, полный
        состав кластера = files ∪ этих трёх списков (PRI-232)."""
        # Мягкая нормализация: тул вызывает LLM, падение на кривом аргументе
        # хуже, чем предсказуемое приведение. limit<=0 == «без лимита».
        page_offset = max(0, int(offset))
        page_limit = int(limit) if limit is not None and int(limit) > 0 else None
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {
                "branch": branch or "",
                "deferred": 0,
                "deferred_files": 0,
                "total_clusters": 0,
                "offset": page_offset,
                "limit": page_limit,
                "has_more": False,
                "clusters": [],
                "note": rb,
            }
        repo, resolved = rb
        state = self._summary_state(
            repo,
            resolved,
            depth=depth,
            min_size=min_size or 1,
            with_centrality=True,
        )
        if not state.members:
            return {
                "branch": resolved,
                "deferred": 0,
                "deferred_files": 0,
                "total_clusters": 0,
                "offset": page_offset,
                "limit": page_limit,
                "has_more": False,
                "clusters": [],
                "note": "(base-индекс пуст — выполните rag-reviewer:sync-codebase)",
            }
        stored = self.components.summary_store.get_source_hashes(repo, resolved)
        generation_flags = {
            cluster.key: self._cluster_generation_flags(state, cluster)
            for cluster in state.clusters
        }
        stale = {
            cluster.key: (
                generation_flags[cluster.key][1]
                or stored.get(cluster.key) != cluster.source_hash
            )
            for cluster in state.clusters
        }
        deltas = {
            cluster.key: self._summary_delta(state, cluster)
            for cluster in state.clusters
        }
        orphans = len(set(stored) - {cluster.key for cluster in state.clusters})
        effective_cap = cap if cap is not None else self.settings.summary_rebuild_cap
        deferred_keys: set[str] = set()
        if effective_cap and effective_cap > 0:
            rebuild_clusters = [
                cluster
                for cluster in state.clusters
                if stale[cluster.key] or generation_flags[cluster.key][0]
            ]
            if len(rebuild_clusters) > effective_cap:
                updated = self.components.summary_store.get_updated_ats(repo, resolved)
                never = [
                    cluster
                    for cluster in rebuild_clusters
                    if cluster.key not in updated
                ]
                aged = sorted(
                    (
                        cluster
                        for cluster in rebuild_clusters
                        if cluster.key in updated
                    ),
                    key=lambda cluster: updated[cluster.key],
                )
                deferred_keys = {
                    cluster.key
                    for cluster in (never + aged)[effective_cap:]
                }
        deferred_files = sum(
            len(deltas[key].pending_paths) for key in deferred_keys
        )
        selected = sorted(
            (cluster for cluster in state.clusters
             if cluster.key not in deferred_keys),
            key=lambda cluster: cluster.key,
        )
        total_clusters = len(selected)
        page_end = (
            total_clusters if page_limit is None else page_offset + page_limit
        )
        page = selected[page_offset:page_end]
        has_more = page_end < total_clusters

        clusters = []
        for cluster in page:
            delta = deltas[cluster.key]
            bootstrap, full_rebuild = generation_flags[cluster.key]
            if compact:
                clusters.append(
                    self._compact_cluster_record(
                        cluster,
                        stale=stale[cluster.key],
                        bootstrap=bootstrap,
                        full_rebuild=full_rebuild,
                        delta=delta,
                    )
                )
                continue
            serialized = self._serialize_summary_delta(
                delta, include_reused_content=False
            )
            # PRI-232: пути delta-списков не дублируются в files — там остаются
            # только неизменённые файлы. Полный состав кластера восстановим как
            # files ∪ added_files ∪ changed_files ∪ moved_files (removed_files в
            # состав уже не входят).
            delta_paths = {
                item["path"]
                for key in ("added_files", "changed_files", "moved_files")
                for item in serialized[key]
            }
            unchanged_files = [
                path for path in cluster.files if path not in delta_paths
            ]
            clusters.append(
                {
                    "cluster_key": cluster.key,
                    "num_members": cluster.num_members,
                    "files": unchanged_files,
                    "top_symbols": cluster.top_symbols,
                    "source_hash": cluster.source_hash,
                    "stale": stale[cluster.key],
                    "added_files": serialized["added_files"],
                    "changed_files": serialized["changed_files"],
                    "removed_files": serialized["removed_files"],
                    "moved_files": serialized["moved_files"],
                    "reused_files": len(delta.reused),
                    "bootstrap": bootstrap,
                    "full_rebuild": full_rebuild,
                }
            )
        return {
            "branch": resolved,
            "depth": state.depth,
            "layout_token": state.layout_token,
            "depth_source": state.depth_source,
            "deferred": len(deferred_keys),
            "deferred_files": deferred_files,
            "orphans": orphans,
            "total_clusters": total_clusters,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": has_more,
            "clusters": clusters,
        }

    def get_subsystem_summary_work(
        self,
        repo: str,
        branch: str,
        cluster_key: str,
        source_hash: str,
    ) -> dict:
        """Вернуть read-only file-level работу для одной актуальной сводки."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"ready": False, "note": rb}
        repo, resolved = rb
        state = self._summary_state(repo, resolved)
        cluster = next(
            (item for item in state.clusters if item.key == cluster_key),
            None,
        )
        if cluster is None or cluster.source_hash != source_hash:
            return {
                "ready": False,
                "note": "состав кластера изменился с момента list",
            }
        bootstrap, full_rebuild = self._cluster_generation_flags(state, cluster)
        delta = self._summary_delta(state, cluster)
        return {
            "ready": True,
            "branch": resolved,
            "cluster_key": cluster.key,
            "source_hash": cluster.source_hash,
            **self._serialize_summary_delta(
                delta, include_reused_content=True
            ),
            "bootstrap": bootstrap,
            "full_rebuild": full_rebuild,
        }

    def _embed_subsystem_summary(
        self,
        repo: str,
        branch: str,
        cluster_key: str,
        title: str,
        summary: str,
        source_hash: str,
    ) -> bool:
        """Вычислить post-commit embedding и записать его через source-hash CAS."""
        try:
            embedding = self.components.embedder.embed_documents(
                [f"{title}\n{summary}"]
            )[0]
            return self.components.summary_store.set_embedding_if_source_hash(
                repo,
                branch,
                cluster_key,
                source_hash,
                embedding,
                title=title,
                summary=summary,
            )
        except Exception:
            log.warning(
                "index_subsystem_summary: сбой post-commit эмбеддинга",
                exc_info=True,
            )
            return False

    def index_subsystem_summary(
        self,
        repo: str,
        branch: str,
        cluster_key: str,
        title: str,
        summary: str,
        source_hash: str,
        fragments: list[SummaryFragmentIn] | list[dict] | None = None,
    ) -> dict:
        """Строго проверить и атомарно сохранить summary с file fragments."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"stored": False, "note": rb}
        repo, resolved = rb
        state = self._summary_state(repo, resolved)
        cluster = next(
            (item for item in state.clusters if item.key == cluster_key),
            None,
        )
        if cluster is None or cluster.source_hash != source_hash:
            return {
                "cluster_key": cluster_key,
                "stored": False,
                "race": True,
                "note": "состав кластера изменился с момента list",
            }

        member_node_ids = cluster.member_node_ids
        metrics: dict[str, int] = {}
        if fragments is None:
            self.components.summary_store.upsert_summary(
                repo,
                resolved,
                cluster_key,
                title,
                summary,
                member_node_ids,
                source_hash,
                embedding=None,
                preserve_embedding=False,
            )
        else:
            try:
                validated = [
                    (
                        fragment
                        if isinstance(fragment, SummaryFragmentIn)
                        else SummaryFragmentIn.model_validate(fragment)
                    )
                    for fragment in fragments
                ]
            except Exception:
                return {
                    "cluster_key": cluster_key,
                    "stored": False,
                    "note": "некорректный payload summary fragments",
                }
            delta = self._summary_delta(state, cluster)
            incoming_paths = [fragment.path for fragment in validated]
            pending_paths = list(delta.pending_paths)
            if (
                len(incoming_paths) != len(set(incoming_paths))
                or sorted(incoming_paths) != sorted(pending_paths)
            ):
                return {
                    "cluster_key": cluster_key,
                    "stored": False,
                    "note": "payload не покрывает ровно все pending summary fragments",
                }
            current_fingerprints = {
                path: state.file_fingerprints[path] for path in cluster.files
            }
            if any(
                current_fingerprints.get(fragment.path) != fragment.fingerprint
                for fragment in validated
            ):
                return {
                    "cluster_key": cluster_key,
                    "stored": False,
                    "note": "fingerprint summary fragment устарел",
                }
            new_fragments = []
            for fragment in validated:
                payload = fragment.model_dump(mode="python")
                payload["provenance"] = with_server_generation_provenance(
                    fragment.provenance,
                    state.depth,
                    state.layout_token,
                )
                new_fragments.append(payload)
            try:
                metrics = self.components.summary_store.commit_summary_bundle(
                    repo,
                    resolved,
                    cluster_key,
                    title,
                    summary,
                    member_node_ids,
                    source_hash,
                    current_fingerprints=current_fingerprints,
                    new_fragments=new_fragments,
                )
            except ValueError as exc:
                return {
                    "cluster_key": cluster_key,
                    "stored": False,
                    "note": str(exc),
                }

        embedded = self._embed_subsystem_summary(
            repo,
            resolved,
            cluster_key,
            title,
            summary,
            source_hash,
        )
        out = {
            "cluster_key": cluster_key,
            "stored": True,
            "members": len(member_node_ids),
            **metrics,
            "embedded": embedded,
        }
        if not embedded:
            out["note"] = (
                "эмбеддинг не сохранён — будет добран бэкфиллом"
            )
        return out

    def _current_subsystem_hashes(
        self, repo: str, branch: str
    ) -> dict[str, str] | None:
        from reviewer.graph.summaries import Member, build_clusters
        from reviewer.index.pathfilter import is_ignored

        try:
            raw = self.components.store.list_base_members(repo, branch)
            if not raw:
                return None
            depth, overrides, ignore, _ = self._resolve_summary_layout(repo, branch)
            members = [
                Member(
                    node_id=f"{path}#{symbol}",
                    path=path,
                    content_hash=content_hash,
                    start_line=start_line,
                    skeleton_hash=skeleton_hash,
                )
                for path, symbol, content_hash, start_line, skeleton_hash in raw
                if not is_ignored(path, ignore)
            ]
            clusters = build_clusters(
                members,
                None,
                depth=depth,
                min_size=1,
                depth_overrides=overrides,
            )
            return {cluster.key: cluster.source_hash for cluster in clusters}
        except Exception:
            log.warning(
                "get_subsystem_summaries: не удалось вычислить свежесть",
                exc_info=True,
            )
            return None

    def _annotate_summary_staleness(
        self, repo: str, branch: str, summaries: list[dict]
    ) -> list[dict]:
        if not summaries:
            return []
        current = self._current_subsystem_hashes(repo, branch)
        return [
            {
                **summary,
                "stale": (
                    None
                    if current is None
                    else summary.get("source_hash") != current.get(summary["cluster_key"])
                ),
            }
            for summary in summaries
        ]

    def get_subsystem_summaries(self, repo: str, branch: str | None = None,
                                cluster_key: str | None = None, query: str | None = None,
                                top_k: int | None = None) -> dict:
        """Дешёвый приор: предрасчитанные summary подсистем (fail-open у потребителя).

        cluster_key → одна сводка. Иначе: при query И числе сводок > порога масштаба
        (effective `summary_topk_threshold` layered policy) — ANN top-k по близости
        (PRI-167); иначе (без query или ≤ порога) — все (бэк-компат)."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {"summaries": [], "note": rb}
        repo, resolved = rb
        store = self.components.summary_store
        if cluster_key:
            summary = store.get_summary(repo, resolved, cluster_key)
            annotated = self._annotate_summary_staleness(
                repo, resolved, [summary] if summary is not None else []
            )
            return {"summary": annotated[0] if annotated else None}
        if query:
            threshold, _ = self._resolve_summary_topk_threshold(repo, resolved)
            if store.count_summaries(repo, resolved) > threshold:
                qvec = self.components.embedder.embed_query(query)
                summaries = store.search_summaries(repo, resolved, qvec, top_k or 8)
                return {
                    "summaries": [{**summary, "stale": None} for summary in summaries]
                }
        summaries = store.get_summaries(repo, resolved)
        return {
            "summaries": self._annotate_summary_staleness(repo, resolved, summaries)
        }

    def prune_subsystem_summaries(
        self,
        repo: str,
        branch: str | None = None,
        layout_token: str | None = None,
        expected_source_hashes: dict[str, str] | None = None,
    ) -> dict:
        """Финализировать только полный подтверждённый snapshot текущего layout."""
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return {
                "completed": False,
                "race": True,
                "deferred": 1,
                "pruned": 0,
                "kept": 0,
                "fragments_pruned": 0,
                "depth": None,
                "layout_token": None,
                "note": rb,
            }
        repo, resolved = rb
        state = self._summary_state(repo, resolved)
        rejected = {
            "completed": False,
            "race": True,
            "deferred": len(state.clusters),
            "pruned": 0,
            "kept": len(state.clusters),
            "fragments_pruned": 0,
            "depth": state.depth,
            "layout_token": state.layout_token,
        }
        if not state.members:
            return {
                **rejected,
                "note": "(base-индекс пуст — purge пропущен)",
            }
        if layout_token is None or expected_source_hashes is None:
            return {
                **rejected,
                "note": "обязателен snapshot из успешного uncapped list",
            }
        if layout_token != state.layout_token:
            return {
                **rejected,
                "note": "layout изменился после list",
            }
        current_source_hashes = {
            cluster.key: cluster.source_hash
            for cluster in state.clusters
        }
        if expected_source_hashes != current_source_hashes:
            return {
                **rejected,
                "note": "source_hash кластера изменился после list",
            }
        current_fingerprints = {
            cluster.key: {
                path: state.file_fingerprints[path]
                for path in cluster.files
            }
            for cluster in state.clusters
        }
        result = self.components.summary_store.prune_verified_layout(
            repo,
            resolved,
            expected_source_hashes,
            current_fingerprints,
            state.depth,
            state.layout_token,
        )
        return {
            **result,
            "kept": len(state.clusters),
        }

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
        embedded = sum(
            store.set_embedding_if_source_hash(
                repo,
                resolved,
                item["cluster_key"],
                item["source_hash"],
                vec,
                title=item["title"],
                summary=item["summary"],
            )
            for item, vec in zip(pending, vecs)
        )
        return {"embedded": embedded}

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
            if v.reason:
                s.verdict_reasons[v.id] = v.reason
            recorded += 1
        return {"recorded": recorded, "unknown_ids": unknown}

    def publish_review(
        self,
        repo: str,
        pr: int,
        summary: str,
        dry_run: bool = False,
        task_key: str | None = None,
        *,
        model: str | None = None,
        model_verify: str | None = None,
        usage: dict | None = None,
        total_cost: float | None = None,
        started_at: datetime | str | None = None,
        steps: list[dict] | None = None,
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

        PRI-209: метаданные LLM-прохода (model, usage, total_cost, steps) и
        started_at передаются клиентом в publish_review и сохраняются в истории.

        Returns:
            Отчёт со счётчиками (posted/invalid/verify_rejected/dropped_by_gate/
            deduped/already_posted/moved_to_summary/capped) и inline.
        """
        if isinstance(started_at, str):
            try:
                started_at = datetime.fromisoformat(started_at)
            except ValueError:
                log.warning("Некорректный формат started_at: %r — игнорируем", started_at)
                started_at = None
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
        metadata = _RunMetadata(
            model=model,
            model_verify=model_verify,
            usage=usage,
            total_cost=total_cost,
            started_at=started_at,
            steps=steps,
        )
        run_id = self._record_history(
            repo, pr, p, list(s.candidates.values()), deduped, asm,
            verify_rejected=verify_rejected,
            dry_run=dry_run, posted=posted, error=error,
            session=s,
            metadata=metadata,
            parsed=parsed,
            kept=kept,
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
        session: _Session,
        metadata: _RunMetadata | None = None,
        parsed: list[Finding] | None = None,
        kept: list[Finding] | None = None,
    ) -> int | None:
        """Записать прогон в историю (fail-soft).

        Гейтится ``settings.review_history``. Любая ошибка истории не валит
        publish — возвращаем None. При сбое публикации (status=error) находки
        записываются с published=False — как в review_service._record_history.

        ``parsed``/``kept`` (survived verify / прошедшие gate) вместе с
        ``session.candidates``/``verdicts``/``verdict_reasons`` и ``deduped``
        передаются в ``account_outcomes``, который строит строку review_findings
        для КАЖДОГО кандидата (verify_rejected/gate_dropped/deduped/
        already_posted/published_*) — воронка исходов полностью персистится.

        PRI-209: метаданные LLM-прохода (model, usage, total_cost, steps) и
        started_at приходят через ``metadata`` из publish_review / сессии и
        сохраняются в истории. started_at уже нормализован в publish_review
        (str → datetime), поэтому здесь работаем только с datetime или None.
        """
        metadata = metadata or _RunMetadata()
        try:
            history = self._review_service._ensure_history()
            if history is None:
                return None
            now = datetime.now(timezone.utc)
            started = metadata.started_at or session.started_at or now
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            duration_ms = max(0, int((now - started).total_seconds() * 1000))
            status = "error" if (error and not dry_run) else "ok"
            comments_inline = len(asm.inline_comments)
            # PRI-156: analyzed — все candidates (до verify-фильтра); грунтовка строк
            # выполнена только для survived, не для отвергнутых candidates.
            findings_analyzed = len({f.fingerprint() for f in analyzed})
            # PRI-209: сливаем серверные шаги сессии с клиентскими. session всегда
            # задан (publish_review → _session, который либо возвращает сессию, либо
            # бросает ValueError), поэтому ветки на session is None здесь нет.
            client_steps = metadata.steps or []
            # Не даём клиентскому списку раздуть слияние с session.steps (пиковое
            # O(N)-выделение). При заполненной сессии max_client == 0 → отбрасываем
            # клиентские шаги ЦЕЛИКОМ: срез client_steps[-0:] вернул бы весь список
            # (отрицательный ноль == ноль), и кэп бы не сработал.
            max_client = max(0, _MAX_SESSION_STEPS - len(session.steps))
            if max_client == 0:
                client_steps = []
            elif len(client_steps) > max_client:
                client_steps = client_steps[-max_client:]
            all_steps = session.steps + client_steps
            if all_steps:
                # Не мутируем шаги клиента/сессии — копируем перед нормализацией seq.
                all_steps = [{**step, "seq": i} for i, step in enumerate(all_steps)]
            run = {
                "repo": repo,
                "pr_number": pr,
                "base_sha": p.prq.base_sha,
                "head_sha": p.prq.head_sha,
                "model": metadata.model or "claude-code",
                "model_verify": metadata.model_verify,
                "dry_run": dry_run,
                "started_at": started,
                "finished_at": now,
                "duration_ms": duration_ms,
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
                "usage": metadata.usage,
                "config_sources": p.config_sources,
                "total_cost": metadata.total_cost,
                "error_text": error or None,
            }
            finding_rows = account_outcomes(
                session.candidates, session.verdicts, session.verdict_reasons,
                parsed or [], kept or [], deduped, asm, p.policy,
            )
            # При сбое публикации (status=error) фактически ничего не ушло:
            # снимаем published, но намеченный outcome сохраняем (что хотели).
            rows = (
                [dict(r, published=False) for r in finding_rows]
                if status == "error" else finding_rows
            )
            return history.record_run(run, rows, steps=all_steps or None)
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

    def _gc_overlays(self) -> None:
        """Оппортунистический GC осиротевших overlay (fail-soft, никогда не бросает).

        Вызывается на каждом prepare_review. _cleanup убирает overlay только когда
        publish_review реально состоялся; если ревью брошено между prepare и publish,
        убрать его больше некому — этот вызов и есть уборщик. Активные сессии процесса
        передаются как живые: их overlay неприкосновенен, даже если persist сессии
        упал fail-soft.

        active_keys ограничены тем же TTL, что и персистнутые сессии (по
        _Session.last_seen_at — последней активности, PRI-212), а не всем
        содержимым self._sessions. _sessions
        вычищается ТОЛЬКО в _cleanup (путь publish) — самый частый сценарий
        брошенного ревью (пользователь отменил скилл, а reviewer-mcp продолжает
        жить) никогда туда не попадает, и без TTL-отсечки ключ оставался бы
        «живым» для GC этого процесса вечно, делая overlay бессмертным.

        Сбой GC не должен мешать ревью — любая ошибка уходит в лог.
        """
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=self.settings.review_session_ttl_hours
            )
            active = {k for k, s in self._sessions.items() if s.last_seen_at > cutoff}
            purge_orphaned_overlays(
                self.components.store,
                self._ensure_session_store(),
                self.settings.review_session_ttl_hours,
                active_keys=active,
            )
        except Exception:
            log.warning("GC осиротевших overlay не удался", exc_info=True)

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
        risk_paths = []
        for item in p.risk_paths:
            patch = p.patches.get(item.path)
            lines = commentable_lines(patch)
            risk_paths.append({
                "path": item.path,
                "status": item.status,
                "reasons": list(item.reasons),
                "patch": patch,
                "commentable_right": sorted(lines["RIGHT"]),
                "commentable_left": sorted(lines["LEFT"]),
            })
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
            "task_board_warnings": list(p.policy.task_board_warnings),
            "task_keys": p.task_keys,
            "skipped_paths": p.skipped_paths,
            "risk_paths": risk_paths,
            "risk_skipped_paths": list(p.risk_skipped_paths),
            "skip_drafts": self.settings.review_skip_drafts,
            "suggestions_mode": self._suggestions_mode(),
        }

    def _suggestions_mode(self) -> str:
        """Режим предложений (apply/text) — передаётся в assemble_review для сборки suggestion-блоков."""
        return self.settings.review_suggestions


def _hostname(url: str) -> str:
    """Хост из URL/DSN для санитайзера канала репорта (PRI-239); мусор → пусто."""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit

        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


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
