"""Сервисный слой MCP-сервера: prepare/search/publish поверх Components.

Состояние сессии (PreparedReview + ToolContext) живёт в процессе сервера
между вызовами prepare_review и publish_review одного PR.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass

from reviewer.app import Components
from reviewer.config.settings import Settings
from reviewer.services.review_service import PreparedReview, ReviewService
from reviewer.tools.code_tools import ToolContext, make_tools
from reviewer.vcs.base import VCSProvider
from reviewer.vcs.diff import commentable_lines

log = logging.getLogger(__name__)


@dataclass
class _Session:
    prepared: PreparedReview
    # Храним ctx, а не готовые tools: make_tools(ctx) пересоздаётся на каждый
    # _invoke_tool-вызов, чтобы seen-дедуп (set внутри make_tools) сбрасывался
    # пер-вызов. Повторный одинаковый вызов отдаёт реальный результат из
    # ctx.cache (пер-сессия), а не заглушку «повтор: результат уже показан выше».
    ctx: ToolContext


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

    def prepare_review(self, repo: str, pr: int) -> dict:
        """Подготовить ревью PR: получить юниты, policy, patches; сохранить сессию.

        При повторном вызове для того же (repo, pr) сессия перезаписывается;
        внутренне созданный VCS старой сессии (без vcs_factory) при этом
        fail-soft закрывается — иначе httpx-клиент утёк бы в долгоживущем
        сервере. Жизненным циклом factory-созданных провайдеров владеет
        фабрика (vcs_factory test-only).
        """
        owner, name = repo.split("/", 1)
        old = self._sessions.get((repo, pr))
        vcs = self._vcs_factory(owner, name) if self._vcs_factory else None
        prepared = self._review_service.prepare(owner, name, pr, vcs_provider=vcs)
        ctx = self._tool_context(prepared)
        self._sessions[(repo, pr)] = _Session(prepared, ctx)
        # Старую сессию прибираем ПОСЛЕ успешного prepare: при сбое подготовки
        # она остаётся рабочей. Закрываем только внутренне созданный провайдер.
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
            read_file_fn=(
                (lambda p: prepared.vcs.get_file_at_ref(p, prepared.prq.head_sha))
                if prepared.vcs else None
            ),
            patches=prepared.patches,
            store=getattr(self.components.retriever, "store", None),
            cache={},
        )

    def _session(self, repo: str, pr: int) -> _Session:
        """Получить сессию или бросить ValueError с понятным сообщением."""
        s = self._sessions.get((repo, pr))
        if s is None:
            raise ValueError(
                f"Сессия для {repo}#{pr} не найдена — сначала вызови prepare_review"
            )
        return s

    def _invoke_tool(self, repo: str, pr: int, name: str, args: dict) -> str:
        """Вызов инструмента с per-вызов пересозданием make_tools.

        seen-дедуп сбрасывается на каждый вызов (повтор отдаёт результат из
        ctx.cache, а не заглушку «повтор»); сам кэш живёт всю сессию.
        """
        s = self._session(repo, pr)
        tools = {t.name: t for t in make_tools(s.ctx)}
        return tools[name].invoke(args)

    def search_code(self, repo: str, pr: int, query: str) -> str:
        """Семантико-лексический поиск кода по индексу PR."""
        return self._invoke_tool(repo, pr, "search_code", {"query": query})

    def get_related_symbols(self, repo: str, pr: int, node_id: str) -> str:
        """Связанные символы (вызовы/реализации/тесты) для node_id вида 'path#fqn'."""
        return self._invoke_tool(repo, pr, "get_related_symbols", {"node_id": node_id})

    def read_file(self, repo: str, pr: int, path: str, start: int = 1, end: int = 400) -> str:
        """Точный исходник файла на head-ревизии PR, строки [start..end].

        Дефолты start/end синхронизированы с code_tools.read_file.
        """
        return self._invoke_tool(repo, pr, "read_file", {"path": path, "start": start, "end": end})

    def get_definition(self, repo: str, pr: int, symbol: str) -> str:
        """Где определён символ + его исходный код."""
        return self._invoke_tool(repo, pr, "get_definition", {"symbol": symbol})

    def find_callers(self, repo: str, pr: int, node_id: str) -> str:
        """Кто вызывает символ node_id ('path#fqn') — направленный CALLS (impact-анализ)."""
        return self._invoke_tool(repo, pr, "find_callers", {"node_id": node_id})

    def get_changed_file_diff(self, repo: str, pr: int, path: str) -> str:
        """Дифф другого изменённого файла этого PR."""
        return self._invoke_tool(repo, pr, "get_changed_file_diff", {"path": path})

    def _prepared_payload(self, p: PreparedReview) -> dict:
        """Сериализовать PreparedReview в dict для передачи MCP-клиенту."""
        units = []
        for u in p.units:
            lines = commentable_lines(p.patches.get(u.path))
            units.append({
                "path": u.path,
                "patch": p.patches.get(u.path),
                "commentable_right": sorted(lines["RIGHT"]),
                "commentable_left": sorted(lines["LEFT"]),
            })
        return {
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
                "ignore": p.policy.ignore,
                "output_language": p.policy.output_language,
            },
            "units": units,
            "skipped_paths": p.skipped_paths,
            "skip_drafts": self.settings.review_skip_drafts,
            "suggestions_mode": self._suggestions_mode(),
        }

    def _suggestions_mode(self) -> str:
        """Режим предложений (apply/text) — то же значение, что кладётся в Deps.suggestions_mode."""
        return self.settings.review_suggestions
