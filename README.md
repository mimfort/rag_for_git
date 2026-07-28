# rag-reviewer

[Русский](README.ru.md)

AI-assisted pull-request reviews grounded in whole-repository context: hybrid search, a code
graph, and inline comments anchored to changed lines.

> Requires Python 3.11–3.13 and external Voyage, PostgreSQL/ParadeDB, and Neo4j services.
> Publishing reviews also requires credentials for the selected version-control provider.

[![PyPI](https://img.shields.io/pypi/v/rag-reviewer?color=2563eb&label=PyPI)](https://pypi.org/project/rag-reviewer/)
[![Python 3.11–3.13](https://img.shields.io/badge/python-3.11%E2%80%933.13-2563eb)](https://pypi.org/project/rag-reviewer/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)

## Start here

Choose the shortest route for what you need now. Both routes lead to the same workflows and
reference sections later in this document.

| If you want to… | Follow |
|---|---|
| Try reviewer and get a first result | [Try reviewer](#try-reviewer) |
| Deploy one reviewer service for a team | [Deploy for a team](#deploy-for-a-team) |

## Try reviewer

You need Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), Docker, a Voyage API key, and a VCS
token if reviewer should read or publish pull-request reviews. The stores run locally; embedding
and reranking requests go to Voyage.

1. Install the launcher, start the stores, and configure the server:

   ```bash
   uv tool install --from rag-reviewer reviewer
   docker compose up -d
   reviewer init
   ```

2. See the supported AI clients and connect one:

   ```bash
   reviewer install --list
   reviewer install codex
   ```

3. Check dependencies, build the whole-repository base index, and inspect its freshness:

   ```bash
   reviewer check
   reviewer index /path/to/repo --ref main
   reviewer status /path/to/repo --branch main --json
   ```

   `reviewer check` should report ready credentials and services. The status payload should show
   an indexed SHA and `drift == 0`. Full indexing sends code chunks to Voyage and can be slow on
   its free tier. Without a base index, PR review has only the diff/overlay and therefore thinner
   repository context.

4. Open a new client session and run the first review:

   ```text
   # Claude Code
   /rag-reviewer:reviewer_review-pr owner/repo#123 --dry-run

   # Codex
   $rag-reviewer:reviewer_review-pr owner/repo#123
   ```

   Invocation syntax differs by client. A dry run returns grounded findings without publishing;
   publishing requires VCS write credentials and confirmation in the skill workflow.

For a temporary launcher without a persistent tool installation:

```bash
uvx --from rag-reviewer@latest reviewer
```

## Deploy for a team

A shared deployment consists of one reviewer MCP server plus PostgreSQL/ParadeDB, Neo4j, Voyage,
and selected VCS or task-board providers. MCP (Model Context Protocol) exposes reviewer tools to
AI clients. Keep provider credentials in the server environment; clients send only non-secret
repository, branch, project, and `provider_options`.

1. **Start the stores and configure secrets.**

   ```bash
   docker compose up -d
   reviewer init
   reviewer check
   ```

2. **Choose repository and branch scope.** Set `DEFAULT_REPO` and the ordered
   `REVIEW_BRANCHES` allowlist in server env. Put repository-specific policy, ignored paths,
   context limits, and non-secret board metadata in `.review.yml`.

3. **Build and verify every tracked branch.**

   ```bash
   reviewer index /srv/rag_for_git --ref main --repo mimfort/rag_for_git
   reviewer status /srv/rag_for_git --branch main --json
   ```

4. **Connect team clients.**

   ```bash
   reviewer install --all
   reviewer install codex --dry-run
   ```

   `--dry-run` reports planned config writes. Open a new chat or CLI session afterwards; IDE
   integrations may also require Reload Window.

5. **Add optional board context.** Select a registered provider in `.review.yml`, keep its
   credentials in server-side env, and validate the project scope with `reviewer check`. See
   [Task boards](#task-boards) and the
   [provider reference](docs/board-providers.md).

## Core workflows

Reviewer workflows are delivered as namespaced skills. Each skill defines its own read/write
boundaries and confirmation gates; the MCP server performs storage, graph, VCS, and board work.

### Review a pull request

Use `reviewer_review-pr` for bug finding. It prepares a PR session, retrieves code and graph
context, analyzes changed files, verifies candidate findings, and publishes only grounded results.
Use `--dry-run` first when validating a deployment. Inline comments can target only commentable
diff lines; off-diff findings go to the summary.

### Solve a task

Use `reviewer_solve-task` to turn a board task or free-text request into a persisted brief before
development. It checks index freshness, warms task context, gathers related work and code, then
hands the brief to brainstorming. It does not implement the task by itself.

### Ask a grounded codebase question

Use `reviewer_ask` for onboarding and codebase Q&A. Answers cite real `path:line` locations from
the base index and code graph. It reads and explains; it neither reviews a PR nor modifies code.

### Walk a human reviewer through a PR

Use `reviewer_pr-walkthrough` for a reading guide: where to start, what each file changes, and
which callers are affected. It is intentionally separate from bug review.

### Run a focused review

Use `reviewer_performance-review` for repeated I/O, N+1 work, poor asymptotics, batching, caching,
and memory risks. Use `reviewer_maintainability-review` for complexity, duplication, readability,
separation of concerns, and repository conventions. Both stay within the requested dimension.

### Create and finish board tasks

`reviewer_create-task` drafts a canonical task body and writes only after confirmation.
`reviewer_finish-task` links the PR, moves the task to a discovered done target, adds a task link
to the PR body, and re-syncs the task corpus—also only after confirmation.

### Reviewer grounding in plan/review phases (optional)

[Reviewer grounding in plan/review phases](#reviewer-grounding-in-planreview-phases-optional)
lets planning and review phases reuse session-less reviewer tools when the base index is current.

> **Reviewer grounding (plan/review, optional, fail-open).** Run
> `reviewer status /path/to/repo --branch main --json` first. When `drift == 0`, prefer
> `search_codebase` for cross-file facts and use `callers`, `related_symbols`, `definition`, or
> `implementations` only for central symbols. The base index does not see uncommitted edits, so
> read changed files from disk. If reviewer or the index is unavailable, fall back to local
> search/read tools instead of blocking.

## How it works

RAG means retrieval-augmented generation: the model receives code selected by hybrid semantic and
lexical search instead of only the PR diff. The graph adds structural relationships.

```text
PR → prepare_review → base + overlay retrieval → skill analysis
   → verify → policy gate → grounding → dedup → inline comments + summary → cleanup
```

- **Base index.** Persistent chunks live under `base:<branch>`. PostgreSQL/ParadeDB combines
  pgvector ANN with BM25; Voyage produces embeddings and reranks candidates.
- **Overlay.** Changed PR files use an ephemeral `pr:N` ref. Retrieval takes unchanged files from
  base and changed files from overlay.
- **Code graph.** Neo4j nodes use `node_id = path#fqn`. SCIP provides type-aware `CALLS` and
  `IMPLEMENTS`; `auto` falls back to tree-sitter `CALLS` when SCIP is unavailable.
- **Grounded publishing.** Findings must quote real changed code. GitHub suggestions are emitted
  only when the replacement is safely applyable on the RIGHT side of the diff.
- **Idempotency.** Hidden fingerprints prevent reposting the same finding. Overlay/session cleanup
  runs after publication and fail-soft on errors.

For the module-level map and invariants, see [CLAUDE.md](CLAUDE.md).

## Installation and configuration

### Requirements

- Python `>=3.11,<3.14`;
- Docker for the default PostgreSQL/ParadeDB and Neo4j stack;
- Voyage API credentials for embeddings and reranking;
- VCS credentials for PR reads and publication;
- a supported AI client with the reviewer MCP integration.

### Installation and updates

Persistent CLI:

```bash
uv tool install --from rag-reviewer reviewer
reviewer update
```

Temporary/latest invocation:

```bash
uvx --from rag-reviewer@latest reviewer --help
```

`reviewer update` checks before mutating a persistent uv tool installation. Use
`reviewer install CLIENT --dry-run` to inspect integration writes.

### AI clients

Discover clients with `reviewer install --list`; install one client or every detected client:

```bash
reviewer install codex
reviewer install --all
reviewer install-skills codex
```

Codex-specific lifecycle:

```bash
uvx --from rag-reviewer@latest reviewer install codex
uvx --from rag-reviewer@latest reviewer install codex --dry-run
codex plugin list --json
codex mcp list
```

Claude Code global plugin lifecycle:

```bash
uvx --from rag-reviewer@latest reviewer install claude-code
claude plugin list --json
claude plugin marketplace list --json
```

After installation, start a new chat/CLI session; in an IDE, also use Reload Window.

### Required services and credentials

Run `reviewer init` to write the selected env file and `reviewer check` to validate it. Resolution
order is `REVIEWER_ENV_FILE` → `$XDG_CONFIG_HOME/rag-reviewer/.env` → `./.env`.

Important groups:

- Voyage: `VOYAGE_API_KEY`;
- stores: `PG_DSN`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`;
- VCS: provider token plus optional API base;
- repository scope: `DEFAULT_REPO`, `REVIEW_BRANCHES`;
- board credentials: provider-specific env declared in the registry.

Credentials stay server-side. **Credentials are not returned** by board metadata or discovery
tools and must not be placed in `.review.yml`.

### Repositories and branches

`DEFAULT_REPO` identifies the fallback `owner/name`. `REVIEW_BRANCHES` is an ordered CSV allowlist;
the first entry is primary. Each branch has isolated `base:<branch>` chunks and graph nodes.

```bash
reviewer index /path/to/repo --ref main --repo owner/name
reviewer status /path/to/repo --branch main --json
reviewer search "token verification" --branch main
```

Use `reviewer migrate-branches` once when upgrading a legacy unscoped base index.

### Per-repo `.review.yml`

Per-repo policy overrides server defaults and is read from the target/base branch. Typical fields:

```yaml
paths:
  ignore:
    - generated

summary_cluster_depth: 2
summary_topk_threshold: 20

context_limits:
  search_codebase:
    floor: 4
    ceiling: 15
  graph:
    hops: 1
```

Use `reviewer_configure-review` to update context fields without clobbering unrelated keys.

### Task boards

Board selection is generic and registry-driven. Credentials come from server env; `.review.yml`
contains only non-secret metadata:

```yaml
task_board:
  type: <registered-provider>
  project: PRI
  key_pattern: "[A-Z]+-\\d+"
  url_template: "https://tasks.example/{code}"
  create_target: Backlog
  done_target: Done
  options:
    <provider-option>: <discovered-value>
```

The repo block wins; an explicit empty `task_board:` disables board work. If the block is absent,
the server may use a **non-secret deploy-wide fallback**. Calls use configured registry credentials
without returning them.

The server-side flow is **store-first**:

1. `sync_board` enumerates and normalizes tasks, then stores vectors and task-graph metadata under
   `tasks:<type>:<board>`.
2. Skills call `get_task(key, project=...)`; linked tasks/PRs/code come from task context tools.
3. Client models never enumerate the provider directly and never send credentials.

Legacy aliases remain **legacy metadata for older clients** for one compatibility window:
`TASK_BOARD_API_KEY → YOUGILE_API_KEY` and
`TASK_BOARD_API_BASE → YOUGILE_API_BASE`. New deployments should use registry-declared
provider credentials. See [docs/board-providers.md](docs/board-providers.md) for the current
provider matrix, target discovery, options, setup, and credential rotation.

### Observability and tuning

`reviewer serve` exposes review history and traces through the optional web extra. Summary depth,
top-k threshold, graph backend, and retrieval ceilings change cost/recall trade-offs; start with
defaults and tune only after observing real misses or excessive context.

## CLI reference

| Goal | Commands |
|---|---|
| Configure and integrate | `init`, `install`, `install-skills`, `update` |
| Validate environment | `check` |
| Manage indexes | `index`, `status`, `search`, `migrate-branches`, `gc` |
| Run observability UI | `serve` |
| Start MCP directly | `reviewer-mcp` |

Use `reviewer COMMAND --help` for the current option set. `status` does not spend Voyage tokens;
`search` and indexing do.

## Skills reference

The examples below use Claude-style `/rag-reviewer:...` invocation. Codex exposes the same
namespaced skills with `$rag-reviewer:...`.

### `reviewer_review-pr` — full PR review

- **When:** find correctness, security, performance, and maintainability issues in a PR.
- **Invoke:** `/rag-reviewer:reviewer_review-pr owner/repo#123 --dry-run`.
- **Needs:** reviewer MCP, VCS access, stores, and preferably a fresh base index/graph.
- **Reads/writes:** reads PR/code/task context; publishes comments only after the skill gate.
- **Result:** grounded inline comments plus a summary; deterministic publish handles dedup.

### `reviewer_solve-task` — task to development brief

- **When:** start implementation from a key such as `PRI-220` or a free-text request.
- **Invoke:** `/rag-reviewer:reviewer_solve-task PRI-220`.
- **Needs:** reviewer MCP; board context is optional and the pipeline continues board-less.
- **Reads/writes:** reads task/code context and writes one brief under `docs/superpowers/briefs/`.
- **Result:** a compact brief handed to brainstorming; implementation happens in later skills.

### `reviewer_ask` — grounded codebase Q&A

- **When:** ask where code lives or how a subsystem works.
- **Invoke:** `/rag-reviewer:reviewer_ask how does index freshness work?`.
- **Needs:** a built base index and graph.
- **Reads/writes:** reads repository context and local files; does not modify or review code.
- **Result:** a Russian explanation with real `path:line` citations.

### `reviewer_pr-walkthrough` — human reading guide

- **When:** orient a human reviewer without running a bug review.
- **Invoke:** `/rag-reviewer:reviewer_pr-walkthrough owner/repo#123`.
- **Needs:** reviewer MCP, PR access, base index, and graph.
- **Reads/writes:** reads impact/diffs/callers; posts only on explicit request.
- **Result:** centrality-first reading order, per-file summary, and grounded impact notes.

### `reviewer_performance-review` — performance-only review

- **When:** inspect a diff for repeated work, N+1 I/O, asymptotics, batching, caching, or memory.
- **Invoke:** `/rag-reviewer:reviewer_performance-review`.
- **Needs:** a diff/PR or explicit change scope; reviewer context is fail-open.
- **Reads/writes:** reads the selected changes and nearby context; does not publish by itself.
- **Result:** only concrete performance findings, with assumptions stated.

### `reviewer_maintainability-review` — maintainability-only review

- **When:** inspect complexity, readability, duplication, boundaries, and repository conventions.
- **Invoke:** `/rag-reviewer:reviewer_maintainability-review`.
- **Needs:** a diff/PR or explicit change scope plus repository guidance.
- **Reads/writes:** reads changes and nearby patterns; does not change behavior.
- **Result:** focused simplification findings, excluding unrelated correctness/performance advice.

### `reviewer_create-task` — create a canonical board task

- **When:** file a grounded task on the configured board.
- **Invoke:** `/rag-reviewer:reviewer_create-task describe the requested change`.
- **Needs:** registered board config, discovered create target/options, and project credentials.
- **Reads/writes:** reads code for evidence; calls `create_task` only after explicit confirmation.
- **Result:** canonical body, task key/URL, and a refreshed task corpus.

### `reviewer_finish-task` — close a task after its PR

- **When:** a PR exists and the board task should be linked and completed.
- **Invoke:** `/rag-reviewer:reviewer_finish-task PRI-220 https://github.com/owner/repo/pull/123`.
- **Needs:** task key, PR URL, registered board config, and discovered done target/options.
- **Reads/writes:** after explicit confirmation, appends the PR idempotently, updates the task,
  prepends a task backlink to the PR body, and re-syncs.
- **Result:** done state plus `already_closed`/`task_link_added` reporting without duplicate links.

### `reviewer_sync-codebase` — build or update the base index

- **When:** initialize an index, refresh stale code, or rebuild the graph.
- **Invoke:** `/rag-reviewer:reviewer_sync-codebase --path /srv/repo --ref main`.
- **Needs:** git clone, `uvx`, reviewer services, Voyage, and optional SCIP.
- **Reads/writes:** reads the selected git ref and writes branch-scoped vectors/graph nodes.
- **Result:** incremental index report; failures name the missing prerequisite.

### `reviewer_sync-tasks` — warm task vectors and graph

- **When:** synchronize a configured board before task search or solve-task.
- **Invoke:** `/rag-reviewer:reviewer_sync-tasks`.
- **Needs:** use `reviewer init`, configure the selected provider as documented in
  `docs/board-providers.md`, then validate it with `reviewer check`.
- **Reads/writes:** calls idempotent server-side `sync_board`; it reads the board and does not write
  back.
- **Result:** compact counts and per-board warnings; missing config remains board-less/fail-open.

### `reviewer_summarize-subsystems` — GraphRAG subsystem summaries

- **When:** build or refresh the architectural prior used by Q&A and PR walkthroughs.
- **Invoke:** `/rag-reviewer:reviewer_summarize-subsystems`.
- **Needs:** a fresh base index, code graph, reviewer MCP, and confirmation of cluster depth.
- **Reads/writes:** reads cluster symbols and writes grounded summaries to the summary store.
- **Result:** fresh/pruned summaries with deferred and orphan reporting.

### `reviewer_configure-review` — update `.review.yml`

- **When:** tune ignored paths, retrieval limits, summary clustering, or board metadata.
- **Invoke:** `/rag-reviewer:reviewer_configure-review`.
- **Needs:** a git repository; MCP and databases are not required for baseline analysis.
- **Reads/writes:** reads tracked Python structure/history and changes only approved YAML fields.
- **Result:** preserved foreign keys/comments plus exact rebuild guidance.

## Operations

Use `reviewer check` for environment readiness and `reviewer status --json` for per-branch index
health. `reviewer gc` removes orphaned PR overlays and expired persisted sessions. The full
troubleshooting and limitations checklist follows in the final documentation pass.

## Development notes

Unit tests are offline and exclude integration tests by default:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

See [CLAUDE.md](CLAUDE.md) for architecture, project commands, and invariants.

## License

[MIT](LICENSE) © rag_for_git contributors.
