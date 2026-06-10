"""Оркестрация полного прогона ревью PR.

Выделен из CLI (фаза 3.1) — сервис инкапсулирует:
- получение PR и diff,
- синхронизацию индекса (overlay),
- кап ``max_files``,
- запуск LangGraph,
- запись истории,
- cleanup.

CLI остаётся тонкой обёрткой: парсит аргументы и вызывает
``ReviewService(...).run_review(...)``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from reviewer.app import Components
from reviewer.config.settings import Settings
from reviewer.vcs.base import ChangedFile, PullRequest, VCSProvider
from reviewer.vcs.github import GitHubProvider
from reviewer.agent.graph import build_graph
from reviewer.agent.state import Deps, ReviewUnit
from reviewer.agent.analyzer import LLMAnalyzer, LLMVerifier, LLMSynthesizer
from reviewer.policy.policy import ReviewPolicy
from reviewer.index.chunker import chunk_python
from reviewer.index.freshness import build_overlay, update_base
from reviewer.llm.usage import UsageLog
from reviewer.llm.trace import TraceLog
from reviewer.llm.verdicts import VerdictLog
from reviewer.web.history import ReviewHistory

log = logging.getLogger(__name__)


def _hunk_count(patch: str | None) -> int:
    """Число hunks в unified diff; 0 если patch отсутствует."""
    if not patch:
        return 0
    return sum(1 for line in patch.splitlines() if line.startswith("@@"))


def _file_importance_key(file: ChangedFile) -> tuple[int, int]:
    """Ключ сортировки: больше hunks → важнее; при равенстве — длиннее patch."""
    return (-_hunk_count(file.patch), -(len(file.patch) if file.patch else 0))


def _select_changed_files(
    files: list[ChangedFile], max_files: int,
) -> list[ChangedFile]:
    """Оставить только .py-файлы (не удалённые), отсортированные по важности."""
    py_files = [f for f in files if f.path.endswith(".py") and f.status != "removed"]
    py_files.sort(key=_file_importance_key)
    return py_files[:max_files]


@dataclass
class ReviewResult:
    """Результат прогона ревью."""

    state: dict
    usage: UsageLog
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    skipped_paths: list[str] | None
    published_flag: bool | None
    publish_failed: list[str]
    dry_run: bool
    is_draft_skip: bool = False


class ReviewService:
    """Сервис полного прогона ревью PR."""

    def __init__(
        self,
        settings: Settings,
        components: Components,
        history: ReviewHistory | None = None,
    ) -> None:
        self.settings = settings
        self.components = components
        self._history = history
        self._history_owned = history is None

    def _create_vcs_provider(self, owner: str, repo: str) -> GitHubProvider:
        """Создать GitHub-провайдер с retry."""
        return GitHubProvider(
            owner,
            repo,
            token=self.settings.github_token,
            retry_attempts=self.settings.github_retry_attempts,
            retry_backoff_base=self.settings.github_retry_backoff_base,
        )

    def _ensure_history(self) -> ReviewHistory | None:
        """Вернуть хранилище истории (создать при необходимости)."""
        if self._history is not None:
            return self._history
        if self.settings.review_history:
            self._history = ReviewHistory(
                self.settings.pg_dsn,
                min_size=self.settings.pg_pool_min_size,
                max_size=self.settings.pg_pool_max_size,
            )
            return self._history
        return None

    def run_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        dry_run: bool = False,
        vcs_provider: VCSProvider | None = None,
    ) -> ReviewResult:
        """Выполнить полный прогон ревью для PR.

        Args:
            vcs_provider: опциональный кастомный VCS-провайдер (например,
                для прогона на локальных снапшотах в eval).

        Returns:
            ReviewResult с финальным состоянием графа и метаданными прогона.
        """
        vcs = vcs_provider or self._create_vcs_provider(owner, repo)
        history = self._ensure_history()
        slug = f"{owner}/{repo}"

        try:
            prq = vcs.get_pull_request(pr_number)

            # Драфт-PR пропускаем; записываем в историю со статусом draft_skip
            if prq.draft and self.settings.review_skip_drafts:
                self._record_draft_skip(history, slug, prq, pr_number, dry_run)
                return ReviewResult(
                    state={
                        "review_units": [],
                        "findings": [],
                        "failed_units": [],
                        "verified": [],
                        "summary": "PR — драфт, ревью пропущено.",
                        "inline_comments": [],
                        "published": None,
                    },
                    usage=UsageLog(),
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    duration_ms=0,
                    skipped_paths=None,
                    published_flag=None,
                    publish_failed=[],
                    dry_run=dry_run,
                    is_draft_skip=True,
                )

            files = vcs.get_changed_files(pr_number)

            # Свежесть base-индекса: подтягиваем чанки файлов, изменённых после
            # последней индексации (граф кода обновляется только на reviewer index).
            indexed = self.components.store.get_index_meta("base")
            if indexed and indexed != prq.base_sha:
                try:
                    diff_files = vcs.compare_files(indexed, prq.base_sha)
                    update_base(
                        self.components.store,
                        self.components.embedder,
                        "",
                        prq.base_ref,
                        [f.path for f in diff_files if f.status != "removed"],
                        read=lambda p: vcs.get_file_at_ref(p, prq.base_sha),
                        removed_files=[f.path for f in diff_files if f.status == "removed"],
                    )
                    self.components.store.set_index_meta("base", prq.base_sha)
                    log.info(
                        "Base-индекс синхронизирован: %d файлов (%s..%s)",
                        len(diff_files), indexed[:7], prq.base_sha[:7],
                    )
                except Exception as e:
                    log.warning("Не удалось синхронизировать base-индекс: %s", e)
            elif not indexed:
                log.warning(
                    "SHA base-индекса неизвестен (выполните reviewer index) "
                    "— индекс может быть устаревшим.",
                )

            selected_files = _select_changed_files(
                files, self.settings.review_max_files,
            )
            selected_paths = [f.path for f in selected_files]
            changed = selected_paths

            # Загружаем head-версии выбранных файлов один раз и переиспользуем
            # для overlay и для построения review-юнитов.
            head_sources: dict[str, str] = {}
            for f in selected_files:
                src = vcs.get_file_at_ref(f.path, prq.head_sha)
                if src:
                    head_sources[f.path] = src

            build_overlay(
                self.components.store,
                self.components.embedder,
                pr_number,
                changed,
                head_sources=head_sources,
            )

            units: list[ReviewUnit] = []
            for f in selected_files:
                src = head_sources.get(f.path)
                if not src:
                    continue
                node_ids = [ch.node_id for ch in chunk_python(f.path, src.encode())]
                units.append(
                    ReviewUnit(f.path, node_ids, f.patch or "", new_source=src)
                )

            # Файлы вне лимита попадают в сводку как пропущенные
            all_py_paths = [
                f.path for f in files
                if f.path.endswith(".py") and f.status != "removed"
            ]
            skipped_paths = [
                p for p in all_py_paths if p not in set(selected_paths)
            ] or None

            # sources нужны верификатору для проверки наличия символов
            sources = {u.path: u.new_source for u in units}

            usage = UsageLog()
            trace = TraceLog() if self.settings.review_trace else None
            verdicts = (
                VerdictLog(self.settings.review_verdict_log, pr=pr_number)
                if self.settings.review_verdict_log
                else None
            )

            policy = ReviewPolicy.load(
                self.settings,
                vcs.get_file_at_ref(".review.yml", prq.base_ref),
            )
            changed_status = {f.path: f.status for f in files}

            # Кросс-файловый синтез имеет смысл только при ≥2 файлах в PR
            synthesizer = (
                LLMSynthesizer(
                    self.components.llm_provider,
                    prompt_cache=self.settings.openrouter_prompt_cache,
                    max_tool_result_chars=self.settings.max_tool_result_chars,
                )
                if self.settings.review_synthesis and len(changed) >= 2
                else None
            )

            deps = Deps(
                vcs=vcs,
                retriever=self.components.retriever,
                graph=self.components.graph,
                policy=policy,
                analyzer=LLMAnalyzer(
                    self.components.llm_provider,
                    self.settings.review_max_tool_iterations,
                    prompt_cache=self.settings.openrouter_prompt_cache,
                    max_tool_result_chars=self.settings.max_tool_result_chars,
                ),
                verifier=LLMVerifier(
                    self.components.llm_provider,
                    agentic=self.settings.review_agentic_verify,
                    max_iterations=self.settings.review_verify_max_iterations,
                    min_severity=self.settings.review_verify_min_severity,
                    model=(self.settings.openrouter_model_verify or None),
                    prompt_cache=self.settings.openrouter_prompt_cache,
                    oneshot_threshold=self.settings.verify_oneshot_threshold,
                    max_verify_tokens=self.settings.max_verify_tokens,
                    max_tool_result_chars=self.settings.max_tool_result_chars,
                ),
                pr_number=pr_number,
                head_sha=prq.head_sha,
                overlay_ref=f"pr:{pr_number}",
                changed_paths=changed,
                patches={f.path: f.patch for f in files},
                tool_cache={},
                settings=self.settings,
                suggestions_mode=self.settings.review_suggestions,
                pr_title=prq.title,
                pr_body=prq.body,
                changed_status=changed_status,
                synthesizer=synthesizer,
                sources=sources,
                usage=usage,
                trace=trace,
                verdicts=verdicts,
                skipped_paths=skipped_paths,
            )

            started_at = datetime.now(timezone.utc)
            state = build_graph(deps, publish=not dry_run).invoke(
                {
                    "review_units": units,
                    "findings": [],
                    "failed_units": [],
                    "verified": [],
                    "summary": "",
                    "inline_comments": [],
                },
                config={"max_concurrency": self.settings.review_max_parallel_files},
            )
            finished_at = datetime.now(timezone.utc)
            duration_ms = int(
                (finished_at - started_at).total_seconds() * 1000
            )

            publish_failed = [
                entry for entry in state.get("failed_units", [])
                if entry.startswith("publish:")
            ]
            published_flag = state.get("published")

            # --- запись истории прогона (fail-soft) ---
            if self.settings.review_history and history is not None:
                try:
                    self._record_history(
                        history=history,
                        slug=slug,
                        pr_number=pr_number,
                        prq=prq,
                        dry_run=dry_run,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        units=units,
                        state=state,
                        usage=usage,
                        trace=trace,
                        skipped_paths=skipped_paths,
                        published_flag=published_flag,
                        publish_failed=publish_failed,
                    )
                except Exception as _exc:
                    log.warning(
                        "Не удалось сохранить историю прогона: %s", _exc,
                    )

            return ReviewResult(
                state=state,
                usage=usage,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                skipped_paths=skipped_paths,
                published_flag=published_flag,
                publish_failed=publish_failed,
                dry_run=dry_run,
            )

        finally:
            # Удаляем эфемерный overlay pr:N — он не нужен после завершения ревью
            try:
                self.components.store.delete_ref(f"pr:{pr_number}")
            except Exception:
                log.warning(
                    "Не удалось очистить overlay pr:%s", pr_number, exc_info=True,
                )
            # store и graph НЕ закрываем — caller (CLI/eval) управляет их жизненным циклом
            if history is not None and self._history_owned:
                history.close()
            if vcs_provider is None:
                vcs.close()

    def _record_draft_skip(
        self,
        history: ReviewHistory | None,
        slug: str,
        prq: PullRequest,
        pr_number: int,
        dry_run: bool,
    ) -> None:
        """Записать draft_skip в историю (fail-soft)."""
        if history is None:
            return
        try:
            _now = datetime.now(timezone.utc)
            _run = {
                "repo": slug,
                "pr_number": pr_number,
                "base_sha": prq.base_sha,
                "head_sha": prq.head_sha,
                "model": self.settings.openrouter_model,
                "model_verify": self.settings.openrouter_model_verify or None,
                "dry_run": dry_run,
                "started_at": _now,
                "finished_at": _now,
                "duration_ms": 0,
                "status": "draft_skip",
                "files_reviewed": 0,
                "files_skipped": 0,
                "files_failed": 0,
                "findings_analyzed": 0,
                "findings_kept": 0,
                "verify_rejected": 0,
                "comments_inline": 0,
                "comments_summary": 0,
                "usage": {},
                "total_cost": 0.0,
                "error_text": None,
            }
            history.record_run(_run, [])
        except Exception as _exc:
            log.warning(
                "Не удалось сохранить draft_skip в историю: %s", _exc,
            )

    def _record_history(  # noqa: PLR0913
        self,
        *,
        history: ReviewHistory,
        slug: str,
        pr_number: int,
        prq: PullRequest,
        dry_run: bool,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: int,
        units: list[ReviewUnit],
        state: dict,
        usage: UsageLog,
        trace: TraceLog | None,
        skipped_paths: list[str] | None,
        published_flag: bool | None,
        publish_failed: list[str],
    ) -> None:
        """Сформировать и записать полный прогон в историю."""
        usage_snap = usage.snapshot()
        total_cost = sum(
            v.get("cost", 0.0) for v in usage_snap.values()
        )

        # fingerprint-сет опубликованных inline-комментариев
        inline_fps: set[str] = set()
        for ic in state["inline_comments"]:
            m = re.search(
                r"<!--\s*ai-review:([0-9a-f]+)\s*-->", ic.body,
            )
            if m:
                inline_fps.add(m.group(1))

        analyzed_fps = {f.fingerprint() for f in state["findings"]}
        findings_analyzed = len(analyzed_fps)
        findings_kept = len(state["verified"])
        verify_rejected = max(0, findings_analyzed - findings_kept)
        comments_inline = len(state["inline_comments"])
        comments_summary = max(0, findings_kept - comments_inline)

        status = "error" if (published_flag is False and not dry_run) else "ok"
        error_text = (
            publish_failed[0]
            if (status == "error" and publish_failed)
            else None
        )

        run_dict = {
            "repo": slug,
            "pr_number": pr_number,
            "base_sha": prq.base_sha,
            "head_sha": prq.head_sha,
            "model": self.settings.openrouter_model,
            "model_verify": self.settings.openrouter_model_verify or None,
            "dry_run": dry_run,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": status,
            "files_reviewed": len(units),
            "files_skipped": len(skipped_paths or []),
            "files_failed": len(state.get("failed_units", [])),
            "findings_analyzed": findings_analyzed,
            "findings_kept": findings_kept,
            "verify_rejected": verify_rejected,
            "comments_inline": comments_inline,
            "comments_summary": comments_summary,
            "usage": usage_snap,
            "total_cost": round(total_cost, 6),
            "error_text": error_text,
        }

        findings_published = False if status == "error" else True
        findings_list = [
            {
                "file": f.file,
                "line": f.line,
                "category": f.category,
                "severity": f.severity,
                "confidence": f.confidence,
                "message": (f.message or "")[:500],
                "fingerprint": f.fingerprint(),
                "is_real": True,
                "published": findings_published,
                "inline": f.fingerprint() in inline_fps,
            }
            for f in state["verified"]
        ]

        steps = (
            trace.snapshot()
            if (self.settings.review_trace and trace is not None)
            else None
        )
        history.record_run(run_dict, findings_list, steps=steps)
