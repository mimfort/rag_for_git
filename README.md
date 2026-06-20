# rag_for_git

> 🇷🇺 Русская версия: [README.ru.md](README.ru.md)

An agent that automatically reviews pull/merge requests using **RAG + a code graph + Claude Code**.

---

## What it is

Plain linters catch syntax and style but miss **meaning and relationships**: a broken
function contract, the impact of a change on its callers, a removed guard, a contradiction
with an existing test. This agent gives an LLM **the same context a human reviewer has** —
semantic + lexical retrieval over the whole repository, structural code-graph expansion, and
an agentic tool loop — then posts the result back to GitHub as **inline comments on diff lines
plus a summary**.

A single PR review runs as three stages:

**`prepare_review` (MCP)** → **analyze (Claude subagents)** → **`publish_review` (MCP)**

1. **prepare** — `GitHubProvider` pulls the PR (base/head SHA) and changed files; changed `.py`
   files are chunked (tree-sitter) and embedded (Voyage) into an ephemeral overlay `ref="pr:N"`;
   policy and per-file review units are assembled.
2. **analyze** — the Claude Code skill fans out one subagent per file. Each reasons over the diff
   in a tool loop, pulling in whatever code it needs: `search_code`, `get_related_symbols`,
   `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`.
3. **publish** — a deterministic tail: policy gate (category/severity/confidence/paths) → line
   grounding by exact code quote (anti-hallucination) → dedup → assemble (inline vs summary,
   suggestion invariants, fingerprint idempotency, comment cap) → post to GitHub → history record
   → overlay/session cleanup.

> Status: working v1. Target analysis language is **Python**; VCS is **GitHub** (behind a
> `VCSProvider` interface). Proven live: it catches real bugs and sees the impact on calling code
> and existing tests.

## How it works / Architecture

The core is the `reviewer/` library, assembled in `reviewer/app.py::build_components(settings)`
from `Settings` (pydantic-settings, `.env`). Entry points are `reviewer/entrypoints/cli.py` (Click)
and `reviewer/entrypoints/mcp_server.py` (FastMCP). Three pieces work together:

- **RAG (hybrid retrieval).** Postgres/ParadeDB stores code chunks with `pgvector` (HNSW ANN) and
  `pg_search` (BM25). A query embeds with Voyage, runs both ANN and BM25 search, and the result
  lists are merged with **Reciprocal Rank Fusion (RRF)**, then reranked with Voyage `rerank-2.5`.
- **Code graph (SCIP or tree-sitter, Neo4j).** Symbols and their relationships live in Neo4j.
  The graph orchestrator (`graph/backend.py`) picks a backend via `GRAPH_BACKEND`
  (`auto|scip|treesitter`): **SCIP** (`@sourcegraph/scip-python`) gives a precise, type-aware graph
  with `CALLS` + `IMPLEMENTS` edges; **tree-sitter** is a fast fallback with `CALLS`-by-name only.
  Retrieval expands the changed symbols 1–2 hops to surface callers/callees/implementations/tests.
- **Claude Code plugin via MCP.** The `reviewer-mcp` server exposes `prepare_review`,
  `publish_review`, and the agent tools. The Claude Code plugin (`plugin/`) drives the review: it
  calls `prepare_review`, runs analysis subagents against those MCP tools, then calls
  `publish_review`.

**The single key linking RAG and the graph is `node_id = "path#fqn"`** (e.g.
`rag/embedder.py#VoyageEmbedder.embed_query`). Both the chunk in Postgres and the node in Neo4j use
it, so graph expansion and chunk retrieval are stitched together without any mapping table.

**Index freshness: a stable base + a PR overlay.** A full reindex of a large repo is expensive, so
the index keeps a persistent base and layers PR changes on top:

- **`ref="base:<branch>"`** — the persistent index of a tracked branch (e.g. `"base:main"`,
  `"base:master"`). Each tracked branch in `REVIEW_BRANCHES` has its own isolated index. Updated
  incrementally by `reviewer index --ref <branch>` (only changed files are chunked; only chunks
  with a new `content_hash` are re-embedded — embeddings are reused across branches by hash,
  saving Voyage quota).
- **`ref="pr:N"`** — an ephemeral overlay of just the PR's changed files at its HEAD.
- **On a query**: `retrieval = (base:<branch> where path ∉ changed) ∪ overlay`. For changed files
  the agent sees the **new** version; for everything else, the stable base.
- **Multi-branch.** A PR is reviewed against the index of its target branch (`base_ref` from the
  PR). A PR targeting an untracked branch is skipped (`prepare_review` returns
  `{"status":"skipped",...}`). The code graph (Neo4j `:Symbol`) is also branch-scoped via a
  `branch` property, with unique constraint `(repo, branch, id)`.

```
                ┌─────────────────────────── reviewer (core library) ───────────────────────────┐
                │                                                                                 │
  GitHub PR ───▶│  VCSProvider (github.py)  ──diff/files/patches──▶  MCPReviewService             │
  (owner/repo#N)│        ▲  publish inline + summary                       │ prepare_review        │
                │        │                                                 ▼                       │
                │        │                          ┌──────────── retrieval/Retriever ──────────┐ │
                │        │                          │  hybrid search        graph expansion      │ │
                │        │                          │  ┌──────────────┐   ┌───────────────────┐  │ │
                │        │                          │  │ Postgres      │   │ Neo4j             │  │ │
                │        │                          │  │ (ParadeDB)    │   │ Symbol(path#fqn)  │  │ │
                │        │                          │  │ pgvector(HNSW)│   │ -[:CALLS]->        │  │ │
                │        │                          │  │ + pg_search   │   │ (IMPLEMENTS: SCIP) │  │ │
                │        │                          │  │   (BM25, RRF) │   │ expand 1–2 hops    │  │ │
                │        │                          │  └──────┬───────┘   └─────────┬─────────┘  │ │
                │        │                          │   Voyage embed/rerank   tree-sitter graph  │ │
                │        │                          └─────────────────┬─────────────────────────┘ │
                │        │                                            ▼ ContextPack                │
                │        │                       Claude Code subagents (skill /rag-reviewer:reviewer_review-pr)
                │        │                         tools: search_code, get_related_symbols,        │
                │        │                         read_file, get_definition, find_callers, …      │
                │        └─────────────────── publish_review (gate/grounding/dedup/assemble) ◀─────┘
                └─────────────────────────────────────────────────────────────────────────────────┘

  Stores (Docker):  Postgres/ParadeDB (:5433)  ·  Neo4j (:7687)
  External API:     Voyage (embeddings voyage-code-3 + reranker rerank-2.5)
```

For a deeper, code-verified walkthrough of every module and the data flow, see
[README.ru.md](README.ru.md) (Russian).

## One-click install prompt

Copy and paste into any AI coding assistant (Claude Code, Cursor, Gemini CLI, Mimo Code, OpenCode, Kimi Code, Codex, Copilot…):

```
Set up the rag-reviewer MCP server in this AI tool.

Rules:
- Use `uvx --from rag-reviewer@latest reviewer-mcp` to run the server — do NOT pip install, do NOT clone the repo.
- The `bash -lc` wrapper is required on macOS/Linux so uvx is found by GUI tools: command="/bin/bash", args=["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]. On Windows use command="uvx", args=["--from","rag-reviewer@latest","reviewer-mcp"].
- Detect which AI tool you are running in, then write the MCP entry to the correct config file:
  - Claude Code / generic: .mcp.json in project root ({"mcpServers":{"reviewer":{"command":"/bin/bash","args":["-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
  - Cursor: ~/.cursor/mcp.json (same format)
  - Gemini CLI: ~/.gemini/settings.json ({"mcpServers":{"reviewer":{"command":"/bin/bash","args":["-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
  - Antigravity: ~/.gemini/antigravity/mcp_config.json (same mcpServers format)
  - Windsurf: ~/.codeium/windsurf/mcp_config.json (same mcpServers format)
  - Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json on macOS, %APPDATA%\Claude\claude_desktop_config.json on Windows (same mcpServers format)
  - Mimo Code: ~/.config/mimocode/mimocode.json ({"mcp":{"reviewer":{"type":"local","command":["/bin/bash","-lc","uvx --from rag-reviewer@latest reviewer-mcp"],"enabled":true}}})
  - OpenCode: ~/.config/opencode/opencode.json ({"mcp":{"reviewer":{"type":"local","command":["/bin/bash","-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
  - Kimi Code: ~/.kimi-code/mcp.json ({"mcpServers":{"reviewer":{"command":"/bin/bash","args":["-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
  - Codex CLI: ~/.codex/config.toml ([mcp_servers.reviewer] command="/bin/bash" args=["-lc","uvx --from rag-reviewer@latest reviewer-mcp"])
  - VS Code: ~/Library/Application Support/Code/User/mcp.json (key is "servers" not "mcpServers": {"servers":{"reviewer":{"command":"/bin/bash","args":["-lc","uvx --from rag-reviewer@latest reviewer-mcp"]}}})
- After writing the config, run: uvx --from rag-reviewer reviewer check
- Report what config file was written and whether the check passed.
```

---

## Installation

The MCP server is published on PyPI as [`rag-reviewer`](https://pypi.org/project/rag-reviewer/)
and runs via `uvx` — **no clone of this repo required**.

Requirements: Docker, [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (includes `uvx`),
a Voyage API key, a GitHub token.

### Quick setup (recommended, all platforms)

```bash
# 0) Install the reviewer CLI — once, globally
uv tool install rag-reviewer
# uv and uvx are the same binary; installing uv gives you both.
# The MCP server launched by your editor uses uvx @latest and self-updates automatically.

# 1) Infrastructure
curl -O https://raw.githubusercontent.com/mimfort/rag_for_git/main/docker-compose.yml
docker compose up -d          # Postgres/ParadeDB (:5433) + Neo4j (:7687) + web admin (:8000)

# 2) Configure keys and settings interactively
reviewer init
#    Interactive wizard: fills VOYAGE_API_KEY, GITHUB_TOKEN, and optional groups
#    (stores, multi-repo, task board). Re-run any time to update settings.
#    CI / non-interactive: reviewer init --yes  (accepts all defaults silently)

# 3) Register the MCP server (and skills) in your editor/CLI
reviewer install --all        # auto-detect installed clients + install skills
#    or a specific one: reviewer install cursor|vscode|claude-code|windsurf|gemini|antigravity|mimo|opencode|kimi|trae|codex
#    skills go to clients that support them (Gemini/Mimo/Kimi); add --no-skills to skip

# 4) Verify
reviewer check

# Update CLI later:
uv tool upgrade rag-reviewer
```

> **`reviewer install` is cross-platform** (Windows / macOS / Linux). It injects the
> absolute path to `uvx` automatically — no `bash -lc` wrapper needed. The manual
> JSON configs below use `bash -lc` for macOS/Linux only; on Windows use
> `reviewer install` or set `"command": "uvx"` with `"args": ["--from",
> "rag-reviewer@latest", "reviewer-mcp"]` directly.

> **Claude Code: tools work out of the box.** `reviewer install claude-code` also
> writes an allowlist rule `mcp__reviewer__*` into your global
> `~/.claude/settings.json` (`permissions.allow`), so the reviewer MCP tools run in
> **every** project without hitting the `auto`-mode safety classifier — no manual
> settings edits. Being global, it also covers the plugin (marketplace) install,
> where the server is available everywhere but ships no permission grants.

> **Where keys are read from.** The reviewer resolves its `.env` from a fixed
> location, **not** the current working directory — MCP clients launch the server
> with an arbitrary CWD, so a project-local `.env` is unreliable. Lookup order:
> `$REVIEWER_ENV_FILE` → `$XDG_CONFIG_HOME/rag-reviewer/.env` (default
> `~/.config/rag-reviewer/.env`) → `./.env` (handy when running from a repo clone).
> Real environment variables always win over the file, so you can instead pass keys
> via an `"env": { "VOYAGE_API_KEY": "…", "GITHUB_TOKEN": "…" }` block in your MCP
> client config — works in every client.

- **Voyage** (`VOYAGE_API_KEY`): https://dashboard.voyageai.com/ — free token pool; attach a card
  to lift the 3 RPM / 10K TPM limit (charged only beyond the free pool).
- **GitHub** (`GITHUB_TOKEN`): a PAT with *Pull requests: Read and write* + *Contents: Read*
  (fine-grained) or the `repo` scope (classic). Quick option: `gh auth token`.

All other settings have defaults (documented in `.env.example`). `DEFAULT_REPO` (optional) sets
the default `owner/name` for single-repo deployments. `REVIEW_BRANCHES` (optional, CSV, default
`main`) lists the branches to track — each gets its own isolated base index; PRs targeting a
branch outside this list are silently skipped by `prepare_review`. `TASK_BOARD_TYPE` /
`TASK_BOARD_MCP` / `TASK_BOARD_KEY_PATTERN` / `TASK_BOARD_URL_TEMPLATE` (optional) configure the task
board **once for the whole deployment**, so it need not be repeated in every repo's `.review.yml`
(see *Per-repo policy* below).

### Manual setup (alternative)

If you prefer to configure your client config by hand rather than using `reviewer install`:

Each AI coding tool has its own config file. Pick yours:

| Tool | Global config file | Project config | Install guide |
|---|---|---|---|
| **Claude Code** | `/plugin marketplace add` (see below) | `.claude-plugin/` ✓ | — |
| **Cursor** | `~/.cursor/mcp.json` | `.cursor/mcp.json` ✓ | — |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` | — | — |
| **Claude Desktop** | macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`; Windows: `%APPDATA%\Claude\claude_desktop_config.json` | — | — |
| **Antigravity** | `~/.gemini/antigravity/mcp_config.json` | — | — |
| **Mimo Code** | `~/.config/mimocode/mimocode.json` | `.mimocode/mimocode.json` ✓ | [INSTALL.md](.mimocode/INSTALL.md) |
| **OpenCode** | `~/.config/opencode/opencode.json` | `.opencode/opencode.json` ✓ | [INSTALL.md](.opencode/INSTALL.md) |
| **Kimi Code** | `~/.kimi-code/mcp.json` | `.kimi-code/mcp.json` ✓ | [INSTALL.md](.kimi-code/INSTALL.md) |
| **Gemini CLI** | `~/.gemini/settings.json` | `.gemini/settings.json` ✓ | [GEMINI.md](GEMINI.md) |
| **Codex CLI** | `~/.codex/config.toml` | `.codex-plugin/plugin.json` ✓ | [AGENTS.md](AGENTS.md) |
| **Copilot CLI** | — | `.github-copilot/plugin.json` ✓ | — |
| **Trae IDE** | `~/Library/Application Support/Trae/User/mcp.json` | — | — |
| **VS Code** | `~/Library/Application Support/Code/User/mcp.json` (key: `servers`, not `mcpServers`) | — | — |

Files marked ✓ are already present in this repo — if you open rag_for_git as a project in
that tool, the MCP server auto-connects. For a **global install** (works from any project),
add the entry to the corresponding global config file.

The MCP entry format by tool (macOS/Linux — use `reviewer install` on Windows):

**Mimo Code** (`mimocode.json`):
```json
{
  "$schema": "https://mimo.xiaomi.com//config.json",
  "mcp": {
    "reviewer": {
      "type": "local",
      "command": ["/bin/bash", "-lc", "uvx --from rag-reviewer@latest reviewer-mcp"],
      "enabled": true
    }
  }
}
```

**OpenCode** (`opencode.json`):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "reviewer": {
      "type": "local",
      "command": ["/bin/bash", "-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
    }
  }
}
```

**Kimi Code / Cursor / Gemini CLI / Codex CLI / Trae / Claude Desktop / Windsurf / Antigravity** (standard `mcpServers` JSON):
```json
{
  "mcpServers": {
    "reviewer": {
      "command": "/bin/bash",
      "args": ["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
    }
  }
}
```

**VS Code** (`mcp.json` — note: key is `servers`, not `mcpServers`):
```json
{
  "servers": {
    "reviewer": {
      "command": "/bin/bash",
      "args": ["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
    }
  }
}
```

**Codex CLI** (`~/.codex/config.toml`):
```toml
[mcp_servers.reviewer]
command = "/bin/bash"
args = ["-lc", "uvx --from rag-reviewer@latest reviewer-mcp"]
```

After adding, restart the tool — `reviewer` will appear alongside other MCP servers.

#### Claude Code

Two commands, from any project:

```text
/plugin marketplace add mimfort/rag_for_git
/plugin install rag-reviewer@rag-reviewer-marketplace
```

You get:

- **Skills:** `/rag-reviewer:reviewer_review-pr`, `/rag-reviewer:reviewer_solve-task`, `/rag-reviewer:reviewer_sync-codebase`, `/rag-reviewer:reviewer_sync-tasks`
  (plus `/rag-reviewer:reviewer_maintainability-review`, `/rag-reviewer:reviewer_performance-review`, and `/rag-reviewer:reviewer_ask`).
- **MCP server** `reviewer` exposing: `prepare_review`, `publish_review`, `search_code`,
  `get_related_symbols`, `read_file`, `get_definition`, `find_callers`, `get_changed_file_diff`,
  `index_task`, `search_tasks`, `get_task_context`, `search_codebase`.
  Alongside `search_codebase`, session-less graph tools `related_symbols`/`callers`/`definition` (graph traversal without a PR session) are available — used by the `/rag-reviewer:reviewer_ask` skill for grounded codebase Q&A.

> Run `/plugin` to confirm `rag-reviewer` is installed and enabled.

### 3. Install skills globally (optional)

Skills (`reviewer_review-pr`, `reviewer_solve-task`, `reviewer_sync-codebase`, `reviewer_sync-tasks`, `reviewer_performance-review`, `reviewer_maintainability-review`, `reviewer_ask`)
let you invoke the full review workflow with a single command. Without them you can still call MCP
tools directly, but the skills wrap them into a guided flow.

**`reviewer install` already installs them** for clients that support file-based skills (Gemini,
Mimo, Kimi). To (re)install just the skills — or pick a specific client — use:

```bash
uvx --from rag-reviewer reviewer install-skills --all     # all detected skills-capable clients
uvx --from rag-reviewer reviewer install-skills gemini    # a specific one
uvx --from rag-reviewer reviewer install-skills --list    # show targets + directories
```

It downloads the skills from GitHub (no repo clone) and unpacks them into each client's global
skills directory, with a path-traversal guard. Manual fallback (equivalent):

```bash
curl -sL https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz -o /tmp/rag-reviewer.tgz
mkdir -p ~/.gemini/skills
tar xz -C ~/.gemini/skills --strip-components=3 -f /tmp/rag-reviewer.tgz 'rag_for_git-main/plugin/skills'
rm /tmp/rag-reviewer.tgz
```

| Tool | Global skills directory |
|---|---|
| Gemini CLI | `~/.gemini/skills/` |
| Mimo Code | `~/.config/mimocode/skills/` |
| Kimi Code | `~/.kimi-code/skills/` + `extra_skill_dirs` in `~/.kimi-code/config.toml` |
| OpenCode | `~/.config/opencode/skills/` |
| Claude Code | bundled in the plugin (step above) |
| Cursor | project-level via `.cursor-plugin/plugin.json` |

---

That's it. Build the base index (recommended — see [CLI](#cli)) and review a PR (see
[Plugin usage](#plugin-usage)).

## CLI

All CLI commands are available via `uvx --from rag-reviewer <command>`, or after
`pip install rag-reviewer` / `pip install -e ".[dev]"` (for contributors) simply as `reviewer`.

```bash
# Create ~/.config/rag-reviewer/.env from template (fill in VOYAGE_API_KEY + GITHUB_TOKEN).
# Flags: --path FILE (custom location), --force (overwrite existing).
uvx --from rag-reviewer reviewer init

# Register the MCP server (and skills) in installed AI clients automatically (cross-platform).
# --all auto-detects installed clients; or name one: cursor, claude-desktop, claude-code,
# vscode, windsurf, gemini, antigravity, mimo, opencode, kimi, trae, codex.
# Skills are installed for clients that support them (Gemini/Mimo/Kimi); --no-skills to skip.
# Flags: --list, --dry-run, --path FILE, --pin VERSION, --no-latest, --no-skills.
uvx --from rag-reviewer reviewer install --all
uvx --from rag-reviewer reviewer install cursor

# Install only the skills into a client's global skills directory (Gemini/Mimo/Kimi).
# --all auto-detects; --list shows targets + directories; --path overrides the directory.
uvx --from rag-reviewer reviewer install-skills --all

# Check environment readiness: keys, Postgres, Neo4j, GitHub. Prints ✓/✗ per item;
# exits 1 on any problem. Spends no Voyage quota.
uvx --from rag-reviewer reviewer check

# Update rag-reviewer to the latest version from PyPI.
uvx --from rag-reviewer reviewer update

# Build/update the base index of the target branch from a local clone (vectors + graph).
# Done once, then updated incrementally; gives RAG and the graph whole-repo context.
# --repo may be omitted if the local clone's origin remote is GitHub (owner/name derived automatically)
# or DEFAULT_REPO is set in .env.
uvx --from rag-reviewer reviewer index /path/to/repo --ref main --repo owner/name

# Build the index for a second tracked branch (isolated index, same deployment).
uvx --from rag-reviewer reviewer index /path/to/repo --ref master --repo owner/name

# Diagnostic hybrid search over the base index (verify the index works).
# --branch selects which tracked branch's index to search (default: primary branch).
uvx --from rag-reviewer reviewer search "token verification"
uvx --from rag-reviewer reviewer search "token verification" --branch master

# Index health / freshness (does not spend Voyage quota).
uvx --from rag-reviewer reviewer status
uvx --from rag-reviewer reviewer status /path/to/repo --branch dev

# One-time migration: rename legacy ref="base" → "base:<primary>" after upgrading to multi-branch.
uvx --from rag-reviewer reviewer migrate-branches

# Observability web admin (run history, findings) on the host.
uvx --from rag-reviewer reviewer serve   # http://127.0.0.1:8000  (options: --host / --port)

# MCP server (stdio transport) — started automatically by the plugin.
uvx --from rag-reviewer@latest reviewer-mcp
```

Reviewing works even without a prior `index` — context is then limited to the diff and the overlay
(RAG/graph are "thin"). For full whole-repo impact analysis, run `index` against the target branch.

## Plugin usage

With the plugin installed (see [Installation](#installation)) and Claude Code open at the repo root,
call a skill:

```text
/rag-reviewer:reviewer_review-pr owner/repo#42     # review a PR (prepare_review → subagents → publish_review)
/rag-reviewer:reviewer_sync-codebase               # build/update vector store + code graph from local clone
/rag-reviewer:reviewer_sync-tasks                  # warm the task graph & vector store (server-side ETL via sync_board)
/rag-reviewer:reviewer_solve-task <key | free text>  # gather disciplined context for a task, then hand off to dev
```

A typical end-to-end run:

```bash
git clone https://github.com/ORG/REPO /tmp/REPO
reviewer index /tmp/REPO --ref main       # build base index + graph for main
reviewer index /tmp/REPO --ref master     # optionally index a second branch (REVIEW_BRANCHES=main,master)
# in Claude Code (from the repo root):  /rag-reviewer:reviewer_review-pr ORG/REPO#42
```

For a dry run, pass `--dry-run` to the review skill — `publish_review` assembles the full report
without posting to GitHub.

### Per-repo policy

A `.review.yml` file in the **target (base) branch** overrides the env defaults (a PR cannot weaken
its own review — see *Caveats*):

```yaml
categories: { correctness: true, security: true, performance: true, style: false, requirements: true }
severity_threshold: medium
min_confidence: 0.5
paths: { ignore: ["**/migrations/**", "vendor/**"] }
max_comments: 25

# Optional task context: read the task from a board and check requirement compliance.
# The board (MCP) is connected by the user on the Claude Code side; the plugin does not bundle it.
task_board:
  type: yougile          # yougile | jira — selects the skill playbook
  mcp: yougile           # name of the connected board MCP server (tools are mcp__<mcp>__*)
  key_pattern: "[A-Z]+-\\d+"   # optional; matches Yougile PRI-34/ID-34 and Jira PROJ-123
```

**The `task_board` block is a deploy-wide default, not a per-repo requirement.** A board connection
is the same for every repo of one team, so configure it **once** in the reviewer `.env`
(`TASK_BOARD_TYPE` / `TASK_BOARD_MCP` / `TASK_BOARD_KEY_PATTERN` / `TASK_BOARD_URL_TEMPLATE`) and
every repo inherits it — no `.review.yml` needed just for the board. A `task_board` block in a repo's
`.review.yml` **overrides** that default for that repo; an explicit empty `task_board:` **disables**
the board for it. `review-pr` reads this through the policy; `solve-task` reads it via the
`get_board_config` MCP tool (and the board-MCP, LLM-side) as a fallback when the local `.review.yml`
has no block.

**Bulk task sync is server-side, not LLM (`sync_board`).** The `sync-tasks` skill is a thin trigger:
it calls one MCP tool, `sync_board(board, limit, purge_orphaned, keep_with_prs)`, and the reviewer
server enumerates the board over **REST** itself (`reviewer/tasks/boards/`, behind a
`TaskBoardProvider` interface — Yougile is the reference), normalizes each task into a `TaskBrief`
in Python, and indexes it via the existing batch indexer. The LLM passes no task text, so a sync
costs O(1) tokens regardless of board size. It is incremental via a per-board timestamp watermark in
`index_meta` (`ref="tasks:<board>"`): a repeat sync touches ~0 tasks; `--limit` disables purge and
the watermark advance. The board REST credentials live only in the reviewer-mcp environment
(`TASK_BOARD_API_KEY` / `TASK_BOARD_API_BASE`). This inverts the "reviewer Python never touches the
board" rule **for bulk sync only** — single-task reads in `solve-task` / `review-pr` still go through
the board-MCP on the LLM side.

## Known limitations & caveats

A factual list of what this does and does not do today.

- **No automatic trigger.** A review is not started on PR open/update. It is a manual skill
  invocation inside Claude Code — there is no GitHub App / webhook / CI integration out of the box.
- **Graph auto-reindex is incremental, not full-precision.** On `prepare_review`, when the base
  branch SHA drifts, the code graph is patched for the changed files (tree-sitter, repo-scoped) in
  the same step that self-heals vector chunks — incoming `CALLS` edges from unchanged callers are
  preserved. Not refreshed until the next manual `reviewer index`: `IMPLEMENTS` edges, outgoing
  `CALLS` into unchanged files, and new incoming `CALLS` from unchanged callers. Full SCIP precision
  is restored by `reviewer index`.
- **Multi-repo via a `repo` discriminator.** One deployment hosts N repositories isolated by a
  `repo` (`owner/name`) column/property across Postgres and Neo4j; each review is scoped to its PR's
  repo (no cross-repo retrieval). Index a repo with `reviewer index <path> --repo owner/name` (or let
  it derive `owner/name` from the git `origin` remote, or set `DEFAULT_REPO`). The task graph
  (`:Task`) is intentionally global, so one task can span PRs across several microservice repos.
  Within a repo, each tracked branch has its own isolated index (`ref="base:<branch>"` in Postgres;
  `branch` property on Neo4j `:Symbol` nodes, unique constraint `(repo, branch, id)`).
- **Language scope: Python only.** The chunker (tree-sitter) and the SCIP backend (`scip-python`)
  are Python-specific. Other languages would go behind the same chunker/`GraphIndexer` interfaces.
- **VCS scope: GitHub only.** Only GitHub implements `VCSProvider`; GitLab/Bitbucket are not
  implemented (the abstraction exists, the providers do not).
- **Graph backend trade-off.** A precise, type-aware graph (`CALLS` + `IMPLEMENTS` edges) requires
  `scip-python` in `PATH`. Without it, the tree-sitter fallback gives `CALLS`-by-name only (no
  `IMPLEMENTS`). Mode is chosen via `GRAPH_BACKEND=auto|scip|treesitter`; in `auto`, a SCIP failure
  silently falls back to tree-sitter with a warning, while `scip` propagates the error.
- **Review surface.** Inline comments are only possible on diff lines (the changed/context lines of a
  hunk); everything else goes into the summary. An applyable `suggestion` block is emitted only under
  safe invariants (`apply` mode, an exact replacement, the whole range inside the RIGHT side of the
  diff, no overlap with other fixes); otherwise the advice is plain text.
- **MCP session is in-process.** State between `prepare_review` and `publish_review` lives in the
  running `reviewer-mcp` process (`_Session` in `MCPReviewService`). Both calls for one PR must hit
  the **same** running server — a restart in between loses the session.
- **Voyage free tier** = 3 RPM / 10K TPM; TPM is the main blocker — a full `reviewer index` of a
  large repo throttles (there is retry/backoff with jitter). A single PR review (overlay + query
  embeddings) fits within the limit.
- **LLM cost.** A review fans out Claude subagents per file — that is real token cost, not free.
- **Observability web admin auth is optional.** Basic auth is enabled only if `WEB_ADMIN_USER` /
  `WEB_ADMIN_PASSWORD` are set; by default it is not hardened for public exposure (it binds to
  loopback in `docker-compose.yml`).
- **GitHub API caps.** The PR file list is paginated by 100; the compare API used to re-sync the base
  index returns at most 300 files — very large diffs are truncated.
- **`.review.yml` comes from the base branch** (by design — a PR cannot weaken its own review), not
  from the PR head.

## Tests

```bash
.venv/bin/pytest -q                 # unit: fast, on fakes; never hit external APIs
.venv/bin/pytest -m integration     # integration: needs running Postgres/Neo4j + a Voyage key
```

`pytest` excludes integration tests by default (`addopts = -m 'not integration'`). External services
(GitHub, Voyage, Postgres, Neo4j) are isolated behind interfaces and mocked in unit tests; real calls
happen only in integration/E2E.

## Project layout

```
reviewer/
  config/      Settings (pydantic-settings): env → review thresholds, stores
  vcs/         VCSProvider + github.py (httpx) · diff.py (lines available for inline)
  index/       chunker(tree-sitter) · embeddings(Voyage) · reranker · store(pgvector+pg_search/RRF) · freshness
  graph/       builder(tree-sitter call-graph) · scip(SCIP parser) · backend(backend orchestrator) · store(Neo4j)
  retrieval/   Retriever: hybrid + graph expansion + rerank → ContextPack
  tools/       agent tools (search_code, get_related_symbols, read_file, get_definition, …)
  agent/       state (ReviewUnit) · assemble · dedup
  mcp/         MCPReviewService: prepare / tool calls / publish; session management
  services/    ReviewService.prepare: ingest PR, overlay, units
  policy/      ReviewPolicy: env defaults + .review.yml + gating
  entrypoints/ cli.py (index / search / check / serve) · mcp_server.py (FastMCP)
  web/         FastAPI + React/Vite SPA — observability web admin
  app.py       dependency assembly from Settings
plugin/        Claude Code plugin (skills /rag-reviewer:reviewer_review-pr, reviewer_solve-task, reviewer_sync-codebase, reviewer_sync-tasks)
docker-compose.yml   ParadeDB (pgvector+pg_search) + Neo4j + web admin
```
