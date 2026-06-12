"""MCP-сервер reviewer-mcp: RAG + граф кода + публикация ревью для Claude Code.

Запускается в stdio-режиме (транспорт по умолчанию FastMCP).
ВАЖНО: logging.basicConfig пишет в stderr, а не в stdout — протокол MCP использует
stdout для JSON-RPC-фреймов, любая запись туда ломает сессию.
"""
import logging

from mcp.server.fastmcp import FastMCP

from reviewer.mcp.service import MCPReviewService

log = logging.getLogger(__name__)


def create_server(service: MCPReviewService) -> FastMCP:
    """Создать и вернуть сконфигурированный FastMCP-сервер с 8 тулами.

    Все тулы — обычные def (sync), а не async: сервис не потокобезопасен
    и рассчитан на последовательное исполнение sync-тулов FastMCP в event loop.
    """
    mcp = FastMCP("reviewer-mcp")

    @mcp.tool()
    def prepare_review(repo: str, pr: int) -> dict:
        """Prepare a PR review session: sync base index, build overlay, load policy.
        Returns PR metadata, policy and review units (path, patch, commentable lines).
        Call this first, before any other tool."""
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
        """Read exact source lines of a file at the PR head revision."""
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
    def publish_review(
        repo: str,
        pr: int,
        summary: str,
        findings: list[dict],
        dry_run: bool = False,
    ) -> dict:
        """Deterministic publish tail: policy gate, line grounding, dedup,
        inline/summary split, suggestion invariants, fingerprint idempotency,
        comment cap, GitHub review post, history record, overlay cleanup.
        With dry_run=true nothing is posted; the full report is returned."""
        return service.publish_review(repo, pr, summary, findings, dry_run)

    return mcp


def main() -> None:
    # logging.basicConfig по умолчанию пишет в stderr — не в stdout,
    # иначе JSON-RPC-фреймы MCP-протокола в stdio-режиме будут повреждены.
    logging.basicConfig(level=logging.INFO)
    from reviewer.app import build_components
    from reviewer.config.settings import Settings

    settings = Settings()
    components = build_components(settings)
    create_server(MCPReviewService(settings, components)).run()  # stdio


if __name__ == "__main__":
    main()
