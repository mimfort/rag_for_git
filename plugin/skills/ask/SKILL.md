---
name: reviewer_ask
description: Answer grounded questions about the codebase (onboarding / Q&A) using the reviewer RAG + code graph. Use when the user asks how the code works, where something lives, or to explain a subsystem ("where is auth", "how does X work", "explain the indexing flow", "как устроено…", "где у нас…", "объясни код"). Requires a built base index + graph (reviewer MCP server). Not for reviewing PRs.
---

# Ask (codebase Q&A)

Answer a free-text question about the codebase with a **grounded** response: every claim is
backed by a real `path:line` citation, never an invented path. Built on the reviewer MCP server
(hybrid RAG + code graph). This skill reads and explains; it does NOT modify code or review PRs.

**Always answer the user in Russian** (the project language), regardless of this file's language.
Tool calls, code identifiers, and `path:line` citations stay verbatim.

## Inputs

`$ARGUMENTS` — a free-text question about the code (e.g. "where is authentication",
"how does index freshness work", "explain the retrieval pipeline").

## Tools

Session-less reviewer MCP tools (no PR / `prepare_review` needed):
- `search_codebase(repo, query, top_k?, branch?)` — hybrid semantic+lexical search; already
  includes 1-hop graph expansion + rerank. Returns chunks headed by `path#fqn (path:start-end)`.
- `related_symbols(repo, node_id, branch?)` — graph neighbors (calls/implements/tests) of a
  `node_id` ('path#fqn').
- `callers(repo, node_id, branch?)` — direct callers of a `node_id` (impact).
- `definition(repo, symbol, branch?)` — where a symbol is defined + its source.

Plus the harness file tools (`Read`, `Grep`, `Glob`) to read source from the local clone on disk.

## Pipeline

1. **Resolve repo/branch.**
   - `repo`: `git remote get-url origin` → strip a trailing `.git`, take the last two path
     segments (`owner/name`). Pass `""` to let the server use `DEFAULT_REPO` if origin is missing.
   - `branch`: `git branch --show-current`. Pass it only if it is in `REVIEW_BRANCHES`; if the
     user named a branch, use that; otherwise omit it (the server uses the primary branch).

2. **Search.** Call `search_codebase(repo, "<question>", branch=…)`. Parse the
   `path#fqn (path:start-end)` headers to get candidate symbols (`node_id`) and line ranges.
   If the result is `(ничего не найдено)`, go to Fallback.

3. **Expand (only as needed).** For an architectural / "how does X work" question, DEFAULT to
   skipping the graph tools (`related_symbols` / `callers` / `definition`) — the hybrid search
   usually suffices; CLAUDE.md / README are cheap priors to consult first. Only when the answer
   genuinely needs call relationships, for the symbols most relevant to the question, call
   `related_symbols` / `callers` / `definition` to follow the graph. Do NOT expand everything —
   only what the answer requires. Stop once you can answer.

4. **Confirm source.** `search_codebase` snippets are line-numbered, so when the returned snippet
   already shows the exact code you cite, you may cite `path:line` directly from the tool output —
   a separate `Read` is not required for grounding. Use `Read` only when the snippet was truncated
   (`[...truncated]`) or you need surrounding context. Never cite a `path:line` not present in any
   tool output.

5. **Answer (adaptive), in Russian.**
   - Default (focused question): a direct answer in 2–4 sentences, then an **Evidence** list —
     each item `path:line` + a one-line "why this is relevant".
   - Broad question ("explain subsystem X"): expand into sections — Краткий ответ / Ключевые
     символы / Поток / Связанные места — each claim still carrying a confirmed `path:line`.

## Grounding contract (hard rule)

Cite ONLY paths that were returned by a tool AND confirmed by the tool's line-numbered output or a `Read`. Never invent or guess a
path or line number. If you cannot ground a statement, say so explicitly instead of fabricating a
citation. This is the skill's acceptance criterion.

A line-numbered `search_codebase` snippet that contains the cited code counts as grounding — an
extra `Read` of the same lines is redundant.

## Fallback (fail-open)

If the reviewer MCP server is unreachable or returns `(ничего не найдено)` / `(граф недоступен)`
(Postgres/Neo4j/index down), degrade gracefully:
- Use the harness `Grep`/`Glob`/`Read` over the local clone to locate and confirm code.
- Tell the user (in Russian) that semantic/graph search was unavailable and the answer comes from
  a lexical search, so it may be less complete.
Never abort — always return the best grounded answer you can.

## Notes

- Precondition: the base index + graph must be built (`reviewer index`). Graph precision depends on
  the backend: SCIP → `IMPLEMENTS` + accurate `CALLS`; tree-sitter → `CALLS` by name only.
- Read-only: this skill never edits code and never posts to GitHub. Posting a human-facing review
  guide to a PR is a separate skill (PRI-119).
