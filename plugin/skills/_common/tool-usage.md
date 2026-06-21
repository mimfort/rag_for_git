Tool discipline (shared):

- Use tools BEFORE claiming cross-file effects.
- Targeted search: make each tool call answer ONE specific question; do not
  browse a file as a whole. Identical calls return cached results instantly;
  still avoid redundant calls — each call should answer a new question. Stop
  calling tools once you can decide.
- If a signature or contract changes, locate all call sites and verify (via
  `read_file` / `get_changed_file_diff`) that they stay consistent.

## PR-session tools (inside `/reviewer_review-pr`)

- `search_code` — usages of a symbol/string;
- `get_related_symbols` — graph neighbours (calls / implementations / tests);
- `find_callers` — callers impacted by a change;
- `get_definition` — a symbol's definition;
- `read_file` — exact source context;
- `get_changed_file_diff` — other changed files of this PR;
- `get_impact` — callers of a changed signature that live outside the diff.

## Session-less tools (`ask` / `solve-task`, no PR session)

- `search_codebase(repo, query, branch?)` — hybrid semantic+lexical search;
- `related_symbols(repo, node_id, branch?)` — graph neighbours;
- `callers(repo, node_id, branch?)` — direct callers (impact);
- `definition(repo, symbol, branch?)` — where a symbol is defined.
