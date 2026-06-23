"""MCP-сервер reviewer-mcp: RAG + граф кода + публикация ревью для Claude Code.

Запускается в stdio-режиме (транспорт по умолчанию FastMCP).
ВАЖНО: logging.basicConfig пишет в stderr, а не в stdout — протокол MCP использует
stdout для JSON-RPC-фреймов, любая запись туда ломает сессию.
"""
import logging
import sys

from mcp.server.fastmcp import FastMCP

from reviewer.mcp.schemas import FindingIn, VerdictIn
from reviewer.mcp.service import MCPReviewService

log = logging.getLogger(__name__)


def create_server(service: MCPReviewService) -> FastMCP:
    """Создать и вернуть сконфигурированный FastMCP-сервер с 29 тулами.

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
        Idempotent: re-embeds only when the task text changed. Returns
        {key, embedded, links_upserted, warnings}."""
        return service.index_task(task)

    @mcp.tool()
    def index_tasks_batch(tasks: list[dict]) -> list[dict]:
        """Batch-index a list of TaskBriefs with a single Voyage embedding call.
        tasks: list of {key, aliases[], title, description, criteria[], status, url, links[]}.
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
                   purge_orphaned: bool = False, keep_with_prs: bool = True) -> dict:
        """Server-side ETL: enumerate the configured task board via REST, normalize,
        and index it (vector store + task graph). The LLM passes no task payload —
        all enumeration/normalization happens server-side, so a sync costs O(1) tokens.
        Incremental via a per-board timestamp watermark; --limit disables purge and
        cursor advance. Returns a compact counts summary (no task text). board filters
        to one project/board by name; purge_orphaned removes tasks no longer on the
        board (keep_with_prs protects tasks with IMPLEMENTED_BY edges)."""
        return service.sync_board(board, limit, purge_orphaned, keep_with_prs)

    @mcp.tool()
    def search_tasks(query: str, top_k: int = 5) -> str:
        """Find semantically similar tasks in the indexed task corpus."""
        return service.search_tasks(query, top_k)

    @mcp.tool()
    def get_task_context(key: str) -> str:
        """Graph context for a task (by key or alias): the task and its PRs,
        linked tasks and their PRs, and the code those PRs touched."""
        return service.get_task_context(key)

    @mcp.tool()
    def get_task(key: str) -> dict | None:
        """Read one task's own normalized content from the reviewer store (filled by
        sync_board): {key, aliases, title, description, status, url, criteria}.
        Store-first single-task read for /solve-task — no board-MCP needed.
        Returns null if the task is not in the store (caller falls back to the board).
        For linked tasks / PRs / touched code use get_task_context instead."""
        return service.get_task(key)

    @mcp.tool()
    def get_board_config() -> dict:
        """Deploy-wide task board config (TASK_BOARD_* env), shared by all repos.
        Returns {"task_board": {type, mcp, key_pattern?, url_template?} | null}.
        Use from /sync-tasks and /solve-task as the fallback when the repo has no
        .review.yml task_board block, so the board need not be duplicated per repo.
        null = no board configured in this deploy."""
        return service.board_config()

    @mcp.tool()
    def search_codebase(repo: str, query: str, top_k: int = 10,
                        branch: str | None = None,
                        include_tests: bool = False) -> str:
        """Hybrid semantic+lexical search over a repo's base code index (no PR session).
        repo is "owner/name" (or "" to use DEFAULT_REPO). branch is a tracked branch
        (REVIEW_BRANCHES); defaults to the primary branch. Results are deduplicated
        (no nested class/method duplicates) and line-numbered for citing path:line
        without a re-Read; test files are excluded unless include_tests=True. Use it
        (e.g. from /solve-task) to find relevant existing code by a free-text formulation."""
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
                                min_size: int | None = None) -> dict:
        """Cluster the base code graph into subsystems (by module path) for the
        /reviewer_summarize-subsystems skill. Returns per cluster: cluster_key,
        num_members, files, top_symbols (by centrality), source_hash, and stale
        (true when the stored summary is missing or its source_hash differs).
        No PR session; branch defaults to the primary tracked branch."""
        return service.list_subsystem_clusters(repo, branch, depth, min_size)

    @mcp.tool()
    def index_subsystem_summary(repo: str, branch: str, cluster_key: str,
                                title: str, summary: str, source_hash: str) -> dict:
        """Persist one subsystem summary (idempotent upsert keyed by
        repo+branch+cluster_key). Called by /reviewer_summarize-subsystems after the
        LLM writes title+summary for a cluster. source_hash ties the summary to the
        cluster's current content for staleness."""
        return service.index_subsystem_summary(
            repo, branch, cluster_key, title, summary, source_hash)

    @mcp.tool()
    def get_subsystem_summaries(repo: str, branch: str | None = None,
                                cluster_key: str | None = None) -> dict:
        """Cheap high-level prior for ask / PR-walkthrough: precomputed subsystem
        summaries. cluster_key=None → all {cluster_key, title, summary}; a cluster_key
        → one full summary (or null). Empty when none built (consumer is fail-open).
        No PR session; branch defaults to primary."""
        return service.get_subsystem_summaries(repo, branch, cluster_key)

    @mcp.tool()
    def publish_review(
        repo: str,
        pr: int,
        summary: str,
        dry_run: bool = False,
        task_key: str | None = None,
    ) -> dict:
        """Опубликовать ревью: verify-фильтр/gate/dedup/assemble из сессии (PRI-156).

        Находки берутся из сессии (накоплены через submit_findings/submit_verdicts).
        When task_key is set and the review is really published, the PR is linked
        to that task in the task graph (IMPLEMENTED_BY + TOUCHES changed code).
        With dry_run=true nothing is posted; the full report is returned."""
        return service.publish_review(repo, pr, summary, dry_run, task_key)

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
        inline comments). Outward-facing: the /reviewer_pr-walkthrough skill calls this
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
    server.run()  # stdio


if __name__ == "__main__":
    main()
