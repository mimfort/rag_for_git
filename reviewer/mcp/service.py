"""Сервисный слой MCP-сервера: prepare/search/publish поверх Components.

Состояние сессии (PreparedReview + ToolContext) живёт в процессе сервера
между вызовами prepare_review и publish_review одного PR.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from reviewer.agent.assemble import AssembledReview, assemble_review, ground_line, snap_to_commentable
from reviewer.agent.dedup import dedup_findings
from reviewer.app import Components
from reviewer.config.settings import Settings
from reviewer.services.review_service import (
    BranchNotTrackedError,
    PreparedReview,
    ReviewService,
)
from reviewer.tasks.graph import PRRef
from reviewer.tools.code_tools import ToolContext, make_tools
from reviewer.vcs.base import Finding, VCSProvider
from reviewer.vcs.diff import commentable_lines

log = logging.getLogger(__name__)


_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})


def _coerce_int(value) -> int | None:
    """int-коэрция LLM-значения: int("42") → 42, None/мусор → None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finding_from_dict(d) -> Finding | None:
    """Собрать Finding из словаря в схеме analyze-промпта.

    Вход приходит от модели — кривые словари штатны, коэрция самодостаточная
    (НЕ зависит от _FindingModel в analyzer). Схема: ``category, severity, file,
    line, code_quote, message, suggestion, fix{start_line,end_line,replacement},
    confidence`` (+опц. ``side``). ``code_quote`` хранится в Finding для fuzzy snap в publish_review.

    Гарантии коэрции:

    - не-dict или dict без ``file`` → None (вызывающий считает invalid);
    - ``line``: int-коэрция, мусор → None;
    - ``confidence``: float-коэрция, None/мусор → 0.5;
    - ``severity`` вне {low,medium,high,critical} → "medium";
    - ``side`` вне {RIGHT,LEFT} → "RIGHT";
    - ``suggestion``: не-строка → None (не попадает в тело комментария как repr);
    - ``fix``: int-коэрция start/end; при мусоре (или нестроковом replacement)
      fix отбрасывается целиком.
    """
    if not isinstance(d, dict) or not d.get("file"):
        return None
    severity = d.get("severity")
    if not isinstance(severity, str) or severity not in _VALID_SEVERITIES:
        severity = "medium"
    side = d.get("side")
    if side not in ("RIGHT", "LEFT"):
        side = "RIGHT"
    try:
        confidence = float(d.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    fix = d.get("fix")
    fix_start = fix_end = replacement = None
    if isinstance(fix, dict):
        fix_start = _coerce_int(fix.get("start_line"))
        fix_end = _coerce_int(fix.get("end_line"))
        replacement = fix.get("replacement")
        if fix_start is None or fix_end is None or not isinstance(replacement, str):
            fix_start = fix_end = replacement = None
    suggestion = d.get("suggestion")
    if not isinstance(suggestion, str):
        suggestion = None
    _cq = d.get("code_quote")
    return Finding(
        category=str(d.get("category") or "correctness"),
        severity=severity,
        file=str(d["file"]),
        line=_coerce_int(d.get("line")),
        side=side,
        message=str(d.get("message") or ""),
        suggestion=suggestion,
        confidence=confidence,
        fix_start=fix_start,
        fix_end=fix_end,
        replacement=replacement,
        code_quote=_cq if isinstance(_cq, str) else None,
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

    def board_config(self) -> dict:
        """Глобальный (env) конфиг доски задач деплоя — для клиентских скилов.

        Скилы sync-tasks/solve-task сначала читают per-repo .review.yml; если
        там нет блока task_board, берут этот глобальный дефолт, чтобы не плодить
        .review.yml в каждом репозитории. ``{"task_board": None}`` = доска в
        деплое не настроена (задайте TASK_BOARD_* в .env reviewer-mcp).
        """
        return {"task_board": self.settings.task_board_default()}

    def search_codebase(self, repo: str, query: str, top_k: int = 10,
                        branch: str | None = None) -> str:
        """Гибрид-поиск по base-индексу репозитория (без PR-сессии) — для /solve-task.

        branch — отслеживаемая ветка (allowlist REVIEW_BRANCHES); по умолчанию
        первичная. Поиск идёт по индексу указанной ветки (base:<branch>).
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
        resolved = branch or self.settings.primary_branch()
        try:
            pack = self.components.retriever.search_base(
                repo, query, top_k=top_k, branch=resolved)
        except Exception:
            log.warning("search_codebase: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
        return pack.as_context() or "(ничего не найдено)"

    def publish_review(
        self,
        repo: str,
        pr: int,
        summary: str,
        findings: list[dict],
        dry_run: bool = False,
        task_key: str | None = None,
    ) -> dict:
        """Детерминированный хвост ревью: gate → grounding → dedup → assemble →
        публикация → история → очистка overlay/сессии.

        ``findings`` — словари в схеме analyze-промпта (вход от LLM). Кривые
        словари не валят publish: запись без ``file`` пропускается (счётчик
        ``invalid`` в отчёте), остальные поля коэрцируются с дефолтами — см.
        :func:`_finding_from_dict`. Сессия (repo, pr) должна быть подготовлена
        ``prepare_review``. Overlay и сессия очищаются ВСЕГДА (даже при сбое
        VCS-публикации) — см. ``_cleanup``.

        При указании ``task_key`` и успешной публикации (не dry_run) автоматически
        линкует PR↔задача↔код в граф задач через ``task_service.link_review``
        (рёбра IMPLEMENTED_BY + TOUCHES затронутых символов).

        Args:
            summary: сводка от модели; к ней добавляется markdown-отчёт assemble.
            dry_run: не публиковать в VCS, только собрать отчёт.
            task_key: канонический ключ задачи (например «ID-1») для авто-линковки.

        Returns:
            Отчёт со счётчиками (posted/invalid/dropped_by_gate/deduped/
            already_posted/moved_to_summary/capped) и inline.
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        p = s.prepared

        # 1) Коэрция LLM-входа (кривой dict без file → скип) и грунтовка строки
        # по дословной цитате (анти-галлюцинация).
        _commentable_cache: dict[str, dict] = {
            path: commentable_lines(patch)
            for path, patch in p.patches.items()
            if patch is not None
        }
        parsed: list[Finding] = []
        invalid = 0
        for d in findings:
            f = _finding_from_dict(d)
            if f is None:
                invalid += 1
                log.warning("publish_review: пропущена некорректная находка: %r", d)
                continue
            f.line = ground_line(p.sources.get(f.file), f.code_quote, f.line)
            if f.line is not None and f.side == "RIGHT" and f.file in _commentable_cache:
                f.line = snap_to_commentable(
                    f.line, f.side, f.code_quote, _commentable_cache[f.file], p.sources.get(f.file, ""),
                )
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
            repo, pr, p, parsed, deduped, asm,
            dropped_by_gate=dropped_by_gate, dry_run=dry_run, posted=posted, error=error,
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
            "invalid": invalid,
            "dropped_by_gate": dropped_by_gate,
            "deduped": len(kept) - len(deduped),
            "already_posted": asm.skipped_existing,
            "moved_to_summary": asm.moved_to_summary,
            "capped": asm.capped,
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
        dropped_by_gate: int,
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
            # Паритет со старым пайплайном: analyzed — по уникальным fingerprint
            # (parsed уже грунтован, но ещё без _sane_line из assemble — допустимо).
            findings_analyzed = len({f.fingerprint() for f in parsed})
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
                # verify_rejected = отсев политикой-gate (в новом пути verify
                # живёт в скилле, до publish доходят уже отфильтрованные находки).
                "verify_rejected": dropped_by_gate,
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
