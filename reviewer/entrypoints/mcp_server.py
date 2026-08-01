"""MCP-сервер reviewer-mcp: RAG + граф кода + публикация ревью для Claude Code.

Запускается в stdio-режиме (транспорт по умолчанию FastMCP).
ВАЖНО: logging.basicConfig пишет в stderr, а не в stdout — протокол MCP использует
stdout для JSON-RPC-фреймов, любая запись туда ломает сессию.
"""
import logging
import sys

from mcp.server.fastmcp import FastMCP

from reviewer.mcp.schemas import FindingIn, SubtasksIn, SummaryFragmentIn, VerdictIn
from reviewer.mcp.service import MCPReviewService

log = logging.getLogger(__name__)


def create_server(service: MCPReviewService) -> FastMCP:
    """Создать и вернуть сконфигурированный FastMCP-сервер с 38 тулами.

    Все тулы — обычные def (sync), а не async: сервис не потокобезопасен
    и рассчитан на последовательное исполнение sync-тулов FastMCP в event loop.
    """
    mcp = FastMCP("reviewer-mcp")

    @mcp.tool()
    def prepare_review(repo: str, pr: int) -> dict:
        """Prepare a PR review session: sync base index, build overlay, load policy.
        Returns PR metadata, policy and review units (path, patch, commentable lines).
        repo like "owner/name". Call this first, before any other tool."""
        return service.prepare_review(repo, pr)

    @mcp.tool()
    def search_code(repo: str, pr: int, query: str) -> str:
        """Hybrid semantic+lexical code search over the repo index (base + PR overlay)."""
        return service.search_code(repo, pr, query)

    @mcp.tool()
    def get_related_symbols(repo: str, pr: int, node_id: str) -> str:
        """Code-graph neighbors of a symbol node_id 'path#fqn'.
        Each item: node_id + (file:line) + one-line definition snippet + edge type
        (CALLS/IMPLEMENTS/TESTED_BY) and hop distance."""
        return service.get_related_symbols(repo, pr, node_id)

    @mcp.tool()
    def read_file(repo: str, pr: int, path: str, start: int = 1, end: int = 400,
                  skeleton: bool = False) -> str:
        """Read source lines of a file at the PR head revision.
        start/end are 1-based inclusive line numbers (full mode).
        skeleton=True returns an AST skeleton (def/class signatures + first docstring line)
        instead of bodies — compact orientation; fetch a full body afterwards with a range."""
        return service.read_file(repo, pr, path, start, end, skeleton)

    @mcp.tool()
    def get_definition(repo: str, pr: int, symbol: str) -> str:
        """Find a symbol definition (graph -> index -> semantic fallback)."""
        return service.get_definition(repo, pr, symbol)

    @mcp.tool()
    def find_callers(repo: str, pr: int, node_id: str) -> str:
        """Direct callers of a symbol node_id.
        Each item: node_id + (file:line) + one-line snippet of the caller's
        definition + [CALLS]."""
        return service.find_callers(repo, pr, node_id)

    @mcp.tool()
    def get_changed_file_diff(repo: str, pr: int, path: str) -> str:
        """Unified diff of another changed file in the same PR."""
        return service.get_changed_file_diff(repo, pr, path)

    @mcp.tool()
    def get_impact(repo: str, pr: int) -> str:
        """Blast-radius: symbols whose signature changed -> their callers outside the PR diff."""
        return service.get_impact(repo, pr)

    @mcp.tool()
    def index_task(task: dict) -> dict:
        """Index a normalized TaskBrief into the task graph + vector store.
        task: {key, aliases[], title, description, criteria[], status, url, links[]}.
        TaskBrief может содержать поле attachments: [{name, mime_type, size, content_text}].
        Idempotent: re-embeds only when the task text changed. Returns
        {key, embedded, links_upserted, warnings}."""
        return service.index_task(task)

    @mcp.tool()
    def index_tasks_batch(tasks: list[dict]) -> list[dict]:
        """Batch-index a list of TaskBriefs with a single Voyage embedding call.
        tasks: list of {key, aliases[], title, description, criteria[], status, url, links[]}.
        TaskBrief может содержать поле attachments: [{name, mime_type, size, content_text}].
        Idempotent: re-embeds only tasks whose text changed. Returns list of
        {key, embedded, links_upserted, warnings} in input order."""
        return service.index_tasks_batch(tasks)

    @mcp.tool()
    def purge_orphaned_tasks(
        active_keys: list[str], keep_with_prs: bool = True
    ) -> dict:
        """Remove tasks no longer on the board from the vector store and task graph.
        active_keys: all current board task keys (canonical ID-N form).
        keep_with_prs: if True (default), tasks with IMPLEMENTED_BY edges are protected.
        Returns {deleted_store, deleted_graph, protected_prs, warnings}."""
        return service.purge_orphaned_tasks(active_keys, keep_with_prs)

    @mcp.tool()
    def sync_board(board: str | None = None, limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True,
                   board_type: str | None = None,
                   provider_options: dict[str, object] | None = None,
                   force_renormalize: bool = False) -> dict:
        """Server-side ETL: enumerate the configured task board via REST, normalize,
        and index it (vector store + task graph). board_type is a registered provider
        type; provider_options is its non-secret JSON options object. board limits the
        operation to one project/board.
        Incremental via a per-(type,board) timestamp watermark; --limit disables purge
        and cursor advance.
        force_renormalize=True ignores the watermark and re-normalizes every task —
        a one-off after normalization rules change (PRI-213); content_hash dedup keeps
        the embedding cost to actually-changed descriptions.
        Returns a compact counts summary with by_board breakdown."""
        return service.sync_board(
            board,
            limit,
            purge_orphaned,
            keep_with_prs,
            board_type,
            provider_options,
            force_renormalize,
        )

    @mcp.tool()
    def finish_task(key: str, pr_url: str, note: str | None = None,
                    mark_done: bool = True, board_type: str | None = None,
                    target: str | None = None,
                    provider_options: dict[str, object] | None = None) -> dict:
        """Close a task on the board after its PR is created (server-side write):
        idempotently append the PR link to the description and mark it done, so the
        task's last-modified bumps and the next sync_board re-indexes the updated task.
        board_type is a registered provider type, target is the selected generic done
        target, and provider_options is a non-secret JSON object. It also appends a
        clickable task link to the PR body (task_link_added; fail-soft, reasons land in
        warnings). Credentials remain server-side; failures are returned safely."""
        return service.finish_task(
            key,
            pr_url,
            note,
            mark_done,
            board_type,
            target,
            provider_options,
        )

    @mcp.tool()
    def create_task(title: str, problem: str = "", steps: list[str] | None = None,
                    criteria: list[str] | None = None, context: str | None = None,
                    board_type: str | None = None, project: str | None = None,
                    target: str | None = None,
                    provider_options: dict[str, object] | None = None) -> dict:
        """Create a task on the board (server-side write) from structured fields.

        The canonical markdown body (Проблема / Что сделать / Критерии приёмки /
        Контекст) is assembled by the server, so every client and model produces the
        same structure; the board-specific markup conversion happens in the provider.
        board_type is a registered provider type, target is a value returned by
        get_board_targets, and provider_options is its non-secret JSON options object.
        Credentials come from env; fail-soft. Returns
        {status, key, url, target_resolved, reindexed, warnings}."""
        return service.create_task(
            title,
            problem,
            steps,
            criteria,
            context,
            board_type,
            project,
            target,
            provider_options,
        )

    @mcp.tool()
    def create_subtasks(
        parent_key: str,
        subtasks: SubtasksIn,
        idempotency_key: str,
        board_type: str | None = None,
        project: str | None = None,
        provider_options: dict[str, object] | None = None,
    ) -> dict:
        """Create and attach a confirmed native-subtask batch with durable idempotency."""
        return service.create_subtasks(
            parent_key,
            [item.model_dump() for item in subtasks],
            idempotency_key,
            board_type,
            project,
            provider_options,
        )

    @mcp.tool()
    def search_tasks(query: str, top_k: int | None = None, project: str | None = None) -> str:
        """Find semantically similar tasks in the indexed task corpus.
        project scopes results to one board project (code prefix, e.g. PRI); empty = all.
        top_k — optional override of the result ceiling; None → default."""
        return service.search_tasks(query, top_k, project=project)

    @mcp.tool()
    def get_task_context(key: str, project: str | None = None) -> str:
        """Graph context for a task (by key or alias): the task and its PRs,
        linked tasks and their PRs, and the code those PRs touched.
        project scopes linked tasks to one board project (code prefix); empty = all."""
        return service.get_task_context(key, project=project)

    @mcp.tool()
    def get_task(key: str, project: str | None = None) -> dict | None:
        """Read one task's own normalized content from the reviewer store (filled by
        sync_board): {key, aliases, title, description, status, url, criteria, attachments, links}.
        project scopes the lookup to one board project (code prefix); empty = all.
        Returns null if the task is not in the store (caller falls back to the board).
        For traversed linked tasks / PRs / touched code, use get_task_context instead."""
        return service.get_task(key, project=project)

    @mcp.tool()
    def count_tasks(project: str | None = None) -> dict:
        """Count indexed :Task nodes in the task graph, scoped to a board project
        (code prefix, e.g. PRI); empty/None = all projects. Read-only, no board call.
        Returns {"count": int}; 0 when the graph is unavailable (caller falls back)."""
        return service.count_tasks(project=project)

    @mcp.tool()
    def get_board_config() -> dict:
        """Deploy-wide non-secret task-board metadata, shared by all repos.
        Returns {"task_board": {type, project?, key_pattern?, create_target?,
        done_target?, options?} | null}; the type is a registered provider type.
        Use from /sync-tasks and /solve-task as the fallback when the repo has no
        .review.yml task_board block, so the board need not be duplicated per repo.
        null = no board configured in this deploy."""
        return service.board_config()

    @mcp.tool()
    def get_board_targets(board_type: str | None = None,
                          project: str | None = None,
                          provider_options: dict[str, object] | None = None) -> dict:
        """Discover normalized board metadata (read-only).
        board_type is a registered provider type and provider_options is a non-secret
        JSON object. Returns {board_type, project, capabilities, targets, options, warnings},
        where capabilities are registry-declared optional provider features, targets are
        {id, label, purposes}, and options are {key, label, required_for, choices}.
        Credentials are never returned; failures are safe for fallback."""
        return service.get_board_targets(board_type, project, provider_options)

    @mcp.tool()
    def search_codebase(repo: str, query: str, top_k: int | None = None,
                        branch: str | None = None,
                        include_tests: bool = False) -> str:
        """Hybrid semantic+lexical search over a repo's base code index (no PR session).
        repo is "owner/name" (or "" to use DEFAULT_REPO). branch is a tracked branch
        (REVIEW_BRANCHES); defaults to the primary branch. Results are deduplicated
        (no nested class/method duplicates) and line-numbered for citing path:line
        without a re-Read; test files are excluded unless include_tests=True. Use it
        (e.g. from /solve-task) to find relevant existing code by a free-text formulation.
        top_k — optional override of the result ceiling; None → ceiling from
        .review.yml/default. Coverage is adaptive (reranker cliff cutoff)."""
        return service.search_codebase(repo, query, top_k, branch, include_tests)

    @mcp.tool()
    def related_symbols(repo: str, node_id: str, branch: str | None = None) -> str:
        """Code-graph neighbors (calls/implementations/tests) of a symbol node_id
        'path#fqn' over the base index (no PR session). Each item: node_id +
        (file:line) + one-line definition snippet + edge type and distance.
        branch defaults to the primary tracked branch."""
        return service.related_symbols(repo, node_id, branch)

    @mcp.tool()
    def callers(repo: str, node_id: str, branch: str | None = None) -> str:
        """Direct callers of a symbol node_id 'path#fqn' over the base index
        (no PR session). Each item: node_id + (file:line) + one-line snippet of
        the caller's definition + [CALLS]. branch defaults to the primary tracked branch."""
        return service.callers(repo, node_id, branch)

    @mcp.tool()
    def implementations(repo: str, node_id: str, branch: str | None = None) -> str:
        """Implementers/subclasses of a symbol node_id 'path#fqn' over the base
        index (incoming IMPLEMENTS, no PR session). A class node -> its subclasses;
        a method node -> its overrides. Each item: node_id + (file:line) + one-line
        definition snippet + [IMPLEMENTS]. Accurate after a full `reviewer index`
        with SCIP. branch defaults to the primary tracked branch."""
        return service.implementations(repo, node_id, branch)

    @mcp.tool()
    def definition(repo: str, symbol: str, branch: str | None = None) -> str:
        """Find a symbol definition over the base index (graph -> index -> semantic
        fallback), no PR session. branch defaults to the primary tracked branch."""
        return service.definition(repo, symbol, branch)

    @mcp.tool()
    def get_pr_diff(repo: str, number: int) -> str:
        """Unified diff of a (possibly historical) GitHub PR's changed files, no PR session.
        repo is "owner/name", number is the PR number. Use it (e.g. from /solve-task) to
        lazily inspect what a related task's PR changed. Capped; fail-soft."""
        return service.get_pr_diff(repo, number)

    @mcp.tool()
    def list_subsystem_clusters(repo: str, branch: str | None = None,
                                depth: int | None = None,
                                min_size: int | None = None,
                                cap: int | None = None) -> dict:
        """Кластеризовать base-граф кода по путям модулей для скилла
        rag-reviewer:summarize-subsystems. Возвращает
        {branch, depth, layout_token, deferred, deferred_files, clusters:[...]},
        где layout_token — обязательная canonical identity default depth +
        normalized overrides для последующего verified prune. Каждый кластер
        содержит cluster_key, num_members, files, top_symbols
        (по центральности), source_hash, stale и file-level delta. Без PR-сессии;
        branch по умолчанию — первичная отслеживаемая ветка.

        cap (по умолчанию — env SUMMARY_REBUILD_CAP; None/0 = без ограничений)
        отбрасывает наименее приоритетные stale-кластеры за один проход: сначала
        кластеры без сводки, затем с наиболее старым updated_at. Отложенные
        кластеры не попадают в clusters, но учитываются в deferred — количестве
        задержанных этим проходом кластеров."""
        return service.list_subsystem_clusters(repo, branch, depth, min_size, cap)

    @mcp.tool()
    def get_subsystem_summary_work(repo: str, branch: str, cluster_key: str,
                                   source_hash: str) -> dict:
        """Вернуть file-level delta и тексты переиспользуемых fragments для
        актуального кластера. Read-only; stale source_hash возвращает ready=false."""
        return service.get_subsystem_summary_work(
            repo, branch, cluster_key, source_hash
        )

    @mcp.tool()
    def index_subsystem_summary(repo: str, branch: str, cluster_key: str,
                                title: str, summary: str, source_hash: str,
                                fragments: list[SummaryFragmentIn] | None = None) -> dict:
        """Сохранить сводку подсистемы и опциональные file fragments.
        source_hash и fingerprints проходят строгую optimistic-проверку."""
        return service.index_subsystem_summary(
            repo, branch, cluster_key, title, summary, source_hash, fragments)

    @mcp.tool()
    def get_subsystem_summaries(repo: str, branch: str | None = None,
                                cluster_key: str | None = None,
                                query: str | None = None,
                                top_k: int | None = None) -> dict:
        """Cheap high-level prior for ask / PR-walkthrough: precomputed subsystem
        summaries. cluster_key → one full summary (or null). Otherwise: with `query`
        AND when the summary count exceeds the scale threshold (SUMMARY_TOPK_THRESHOLD,
        per-repo .review.yml), returns the top-k summaries nearest the query by
        embedding; without `query` or at/below the threshold, returns all (back-compat).
        top_k defaults to 8. Empty when none built (consumer is fail-open).
        No PR session; branch defaults to primary. Every returned summary has source_hash
        and stale: true when its stored hash differs from the current cluster, false when
        it matches, null when freshness is not computed (including the scaled ANN path)
        or unavailable."""
        return service.get_subsystem_summaries(repo, branch, cluster_key, query, top_k)

    @mcp.tool()
    def prune_subsystem_summaries(
        repo: str,
        branch: str | None = None,
        layout_token: str | None = None,
        expected_source_hashes: dict[str, str] | None = None,
    ) -> dict:
        """Verify and finalize one exact uncapped list snapshot.
        Pass layout_token and every {cluster_key: source_hash} from that list response.
        The server re-derives layout/hashes and, under a branch lock, verifies summaries
        plus exact file-fragment coverage before deleting orphans and advancing state.
        Missing/changed/incomplete snapshots return completed=false/race=true without
        deletion. Empty base is also a no-op. No PR session; branch defaults to primary."""
        return service.prune_subsystem_summaries(
            repo,
            branch,
            layout_token,
            expected_source_hashes,
        )

    @mcp.tool()
    def backfill_summary_embeddings(repo: str, branch: str | None = None) -> dict:
        """Self-heal: embed any subsystem summaries with a NULL embedding from their
        stored title+summary (no LLM). Idempotent — a later run embeds nothing.
        Called by rag-reviewer:summarize-subsystems after the LLM pass so older summaries
        become searchable by proximity. Returns {embedded}. No PR session; branch
        defaults to primary."""
        return service.backfill_summary_embeddings(repo, branch)

    @mcp.tool()
    def publish_review(
        repo: str,
        pr: int,
        summary: str,
        dry_run: bool = False,
        task_key: str | None = None,
        model: str | None = None,
        model_verify: str | None = None,
        usage: dict | None = None,
        total_cost: float | None = None,
        started_at: str | None = None,
        steps: list[dict] | None = None,
    ) -> dict:
        """Опубликовать ревью: verify-фильтр/gate/dedup/assemble из сессии (PRI-156).

        Находки берутся из сессии (накоплены через submit_findings/submit_verdicts).
        When task_key is set and the review is really published, the PR is linked
        to that task in the task graph (IMPLEMENTED_BY + TOUCHES changed code).
        With dry_run=true nothing is posted; the full report is returned.

        Args:
            model: Модель LLM, использованная для основного прохода ревью.
            model_verify: Модель LLM, использованная для verify-прохода.
            usage: Статистика использования токенов, например {"input_tokens": N}.
            total_cost: Общая стоимость LLM-вызовов в условных единицах.
            started_at: Время начала ревью в формате ISO 8601 (строка).
            steps: Список выполненных шагов/инструментов для трассировки ревью."""
        return service.publish_review(
            repo, pr, summary, dry_run, task_key,
            model=model, model_verify=model_verify, usage=usage,
            total_cost=total_cost, started_at=started_at, steps=steps,
        )

    @mcp.tool()
    def submit_findings(repo: str, pr: int, findings: list[FindingIn]) -> dict:
        """Сдать находки в сессию (schema-enforced). FastMCP валидирует по FindingIn."""
        return service.submit_findings(repo, pr, [f.model_dump() for f in findings])

    @mcp.tool()
    def submit_verdicts(repo: str, pr: int, verdicts: list[VerdictIn]) -> dict:
        """Сдать вердикты verify в сессию (schema-enforced)."""
        return service.submit_verdicts(repo, pr, [v.model_dump() for v in verdicts])

    @mcp.tool()
    def get_candidate_findings(repo: str, pr: int) -> str:
        """Прочитать накопленных кандидатов с id (для verify)."""
        return service.get_candidate_findings(repo, pr)

    @mcp.tool()
    def post_pr_walkthrough(repo: str, pr: int, markdown: str) -> dict:
        """Post a human-facing PR reading guide (walkthrough) as a PR review comment,
        separate from bug findings (carries a <!-- ai-walkthrough --> marker, empty
        inline comments). Outward-facing: the rag-reviewer:pr-walkthrough skill calls this
        only on explicit user request. Requires an active prepare_review session."""
        return service.post_pr_walkthrough(repo, pr, markdown)

    return mcp


def main() -> None:
    # logging.basicConfig по умолчанию пишет в stderr — не в stdout,
    # иначе JSON-RPC-фреймы MCP-протокола в stdio-режиме будут повреждены.
    logging.basicConfig(level=logging.INFO)
    from reviewer.app import build_components
    from reviewer.config.settings import Settings

    try:
        settings = Settings()
        components = build_components(settings)
        server = create_server(MCPReviewService(settings, components))
    except Exception as e:
        # Одна ясная строка в stderr без сырого traceback (детали — в debug-логе).
        log.debug("Сбой инициализации reviewer-mcp", exc_info=True)
        print(
            f"reviewer-mcp: ошибка инициализации компонентов: {type(e).__name__}: {e} "
            "— проверьте .env и `reviewer check`",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        server.run()  # stdio
    finally:
        close = getattr(components, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
