"""MCP-сервер reviewer-mcp: RAG + граф кода + публикация ревью для Claude Code.

Запускается в stdio-режиме (транспорт по умолчанию FastMCP).
ВАЖНО: logging.basicConfig пишет в stderr, а не в stdout — протокол MCP использует
stdout для JSON-RPC-фреймов, любая запись туда ломает сессию.
"""
import logging
import sys

from mcp.server.fastmcp import FastMCP

from reviewer.mcp.service import MCPReviewService

log = logging.getLogger(__name__)


def create_server(service: MCPReviewService) -> FastMCP:
    """Создать и вернуть сконфигурированный FastMCP-сервер с 13 тулами.

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
        """Code-graph neighbors (calls/implementations) of a symbol node_id 'path#fqn'."""
        return service.get_related_symbols(repo, pr, node_id)

    @mcp.tool()
    def read_file(repo: str, pr: int, path: str, start: int = 1, end: int = 400) -> str:
        """Read exact source lines of a file at the PR head revision.
        start/end are 1-based inclusive line numbers."""
        return service.read_file(repo, pr, path, start, end)

    @mcp.tool()
    def get_definition(repo: str, pr: int, symbol: str) -> str:
        """Find a symbol definition (graph -> index -> semantic fallback)."""
        return service.get_definition(repo, pr, symbol)

    @mcp.tool()
    def find_callers(repo: str, pr: int, node_id: str) -> str:
        """List direct callers of a symbol node_id."""
        return service.find_callers(repo, pr, node_id)

    @mcp.tool()
    def get_changed_file_diff(repo: str, pr: int, path: str) -> str:
        """Unified diff of another changed file in the same PR."""
        return service.get_changed_file_diff(repo, pr, path)

    @mcp.tool()
    def index_task(task: dict) -> dict:
        """Index a normalized TaskBrief into the task graph + vector store.
        task: {key, aliases[], title, description, criteria[], status, url, links[]}.
        Idempotent: re-embeds only when the task text changed. Returns
        {key, embedded, links_upserted, warnings}."""
        return service.index_task(task)

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
    def get_board_config() -> dict:
        """Deploy-wide task board config (TASK_BOARD_* env), shared by all repos.
        Returns {"task_board": {type, mcp, key_pattern?, url_template?} | null}.
        Use from /sync-tasks and /solve-task as the fallback when the repo has no
        .review.yml task_board block, so the board need not be duplicated per repo.
        null = no board configured in this deploy."""
        return service.board_config()

    @mcp.tool()
    def search_codebase(repo: str, query: str, top_k: int = 10,
                        branch: str | None = None) -> str:
        """Hybrid semantic+lexical search over a repo's base code index (no PR session).
        repo is "owner/name" (or "" to use DEFAULT_REPO). branch is a tracked branch
        (REVIEW_BRANCHES); defaults to the primary branch. Use it (e.g. from /solve-task)
        to find relevant existing code by a free-text formulation."""
        return service.search_codebase(repo, query, top_k, branch)

    @mcp.tool()
    def publish_review(
        repo: str,
        pr: int,
        summary: str,
        findings: list[dict],
        dry_run: bool = False,
        task_key: str | None = None,
    ) -> dict:
        """Deterministic publish tail: policy gate, line grounding, dedup,
        inline/summary split, suggestion invariants, fingerprint idempotency,
        comment cap, GitHub review post, history record, overlay cleanup.
        When task_key is set and the review is really published, the PR is linked
        to that task in the task graph (IMPLEMENTED_BY + TOUCHES changed code).
        Each finding: {category, severity(low|medium|high|critical), file, line,
        side(RIGHT|LEFT), code_quote, message, suggestion,
        fix:{start_line,end_line,replacement}|null, confidence:0..1}.
        With dry_run=true nothing is posted; the full report is returned."""
        return service.publish_review(repo, pr, summary, findings, dry_run, task_key)

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
