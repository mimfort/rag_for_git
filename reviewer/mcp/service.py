"""Сервисный слой MCP-сервера: prepare/search/publish поверх Components.

Состояние сессии (PreparedReview + ToolContext) живёт в процессе сервера
между вызовами prepare_review и publish_review одного PR.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from reviewer.agent.assemble import AssembledReview, assemble_review, ground_line
from reviewer.agent.dedup import dedup_findings
from reviewer.app import Components
from reviewer.config.settings import Settings
from reviewer.services.review_service import PreparedReview, ReviewService
from reviewer.tools.code_tools import ToolContext, make_tools
from reviewer.vcs.base import Finding, VCSProvider
from reviewer.vcs.diff import commentable_lines

log = logging.getLogger(__name__)


def _finding_from_dict(d: dict) -> Finding:
    """Собрать Finding из словаря в схеме analyze-промпта.

    Схема: ``category, severity, file, line, code_quote, message, suggestion,
    fix{start_line,end_line,replacement}, confidence`` (+опц. ``side``).
    ``code_quote`` тут не используется (нужен только для грунтовки строки).
    """
    fix = d.get("fix") or {}
    return Finding(
        category=d.get("category", "correctness"),
        severity=d.get("severity", "medium"),
        file=d["file"],
        line=d.get("line"),
        side=d.get("side", "RIGHT"),
        message=d.get("message", ""),
        suggestion=d.get("suggestion"),
        confidence=float(d.get("confidence", 0.5)),
        fix_start=fix.get("start_line"),
        fix_end=fix.get("end_line"),
        replacement=fix.get("replacement"),
    )


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

    def publish_review(
        self,
        repo: str,
        pr: int,
        summary: str,
        findings: list[dict],
        dry_run: bool = False,
    ) -> dict:
        """Детерминированный хвост ревью: gate → grounding → dedup → assemble →
        публикация → история → очистка overlay/сессии.

        ``findings`` — словари в схеме analyze-промпта. Сессия (repo, pr) должна
        быть подготовлена ``prepare_review``. Overlay и сессия очищаются ВСЕГДА
        (даже при сбое VCS-публикации) — см. ``_cleanup``.

        Args:
            summary: сводка от модели; к ней добавляется markdown-отчёт assemble.
            dry_run: не публиковать в VCS, только собрать отчёт.

        Returns:
            Отчёт со счётчиками (posted/dropped_by_gate/deduped/...) и inline.
        """
        s = self._session(repo, pr)
        p = s.prepared

        # 1) Грунтуем строку каждой находки по дословной цитате (анти-галлюцинация).
        parsed: list[Finding] = []
        for d in findings:
            f = _finding_from_dict(d)
            f.line = ground_line(p.sources.get(f.file), d.get("code_quote"), f.line)
            parsed.append(f)

        # 2) Gate (категория/severity/confidence/пути) + dedup.
        kept = [f for f in parsed if p.policy.gate(f)]
        deduped = dedup_findings(kept)

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
        full_summary = summary + ("\n\n" + asm.summary if asm.summary else "")

        # 5) Публикация (если не dry_run).
        error, posted = "", False
        if not dry_run:
            try:
                p.vcs.publish_review(pr, p.prq.head_sha, full_summary, asm.inline_comments)
                posted = True
            except Exception as e:
                error = str(e)

        # 6) История (fail-soft) и очистка overlay/сессии (ВСЕГДА).
        run_id = self._record_history(
            repo, pr, p, parsed, deduped, asm, dry_run=dry_run, posted=posted, error=error,
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
            "dropped_by_gate": len(parsed) - len(kept),
            "deduped": len(kept) - len(deduped),
            "already_posted": asm.skipped_existing,
            "moved_to_summary": asm.moved_to_summary,
        }

    def _record_history(
        self,
        repo: str,
        pr: int,
        p: PreparedReview,
        parsed: list[Finding],
        deduped: list[Finding],
        asm: AssembledReview,
        *,
        dry_run: bool,
        posted: bool,
        error: str,
    ) -> int | None:
        """Записать прогон в историю (fail-soft).

        Гейтится ``settings.review_history``. Любая ошибка истории не валит
        publish — возвращаем None. Стоимость/usage недоступны на этом этапе
        (LLM-вызовы прошли вне сервиса), пишем None.
        """
        history = self._review_service._ensure_history()
        if history is None:
            return None
        try:
            now = datetime.now(timezone.utc)
            status = "error" if (error and not dry_run) else "ok"
            comments_inline = len(asm.inline_comments)
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
                "findings_analyzed": len(parsed),
                "findings_kept": len(deduped),
                "verify_rejected": max(0, len(parsed) - len(deduped)),
                "comments_inline": comments_inline,
                "comments_summary": max(0, len(asm.findings_rows) - comments_inline),
                "usage": None,
                "total_cost": None,
                "error_text": error or None,
            }
            return history.record_run(run, asm.findings_rows, steps=None)
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
            self.components.store.delete_ref(f"pr:{pr}")
        except Exception:
            log.warning("Не удалось очистить overlay pr:%s", pr, exc_info=True)

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
