# Global Client Install Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Make `reviewer install --all` install and verify native global Claude Code and Codex plugins while deterministically configuring every other detected supported client.

**Architecture:** Add a small Claude CLI adapter beside `install_codex.py`. It owns only documented Claude marketplace/plugin/MCP commands and public status parsing. Click routes native Claude and Codex targets before the generic config-plan path; `CLIENTS` remains the registry for config-only clients and file-skill destinations.

**Tech Stack:** Python 3.11+, Click, subprocess, JSON/TOML parsing, pytest, Claude Code public CLI, Codex public CLI.

## Global Constraints

- Claude source is exactly `https://github.com/mimfort/rag_for_git.git`; never use GitHub shorthand because it selects SSH.
- Claude marketplace and plugin scopes are `user`; sparse paths are exactly `.claude-plugin` and `plugin`.
- Native state verification uses public CLI JSON where available; Claude MCP-only verification parses the public `claude mcp get reviewer` text because that command has no JSON option. Never parse private caches.
- `--dry-run` performs no marketplace/plugin mutations and no config writes.
- Real CLI tests use unique temporary `HOME`, `CODEX_HOME`, and `CLAUDE_CONFIG_DIR` from an outside-repository CWD.
- Native failures must be collected and reported by `--all`, never silently masked as success.
- Repair the current 11 repository-wide Ruff violations only in `reviewer/graph/scip.py`, `reviewer/vcs/diff.py`, `tests/graph/test_scip.py`, `tests/index/test_schema.py`, `tests/index/test_store_hybrid.py`, and `tests/integration/test_pipeline.py`; do not make unrelated refactors.

---

### Task 1: Claude CLI adapter and deterministic fake

**Files:**

- Create: `reviewer/install_claude.py`
- Create: `tests/install/fake_claude.py`
- Create: `tests/install/test_claude_install.py`
- Modify: `plugin/.mcp.json`

**Interfaces:**

- Consumes `CommandResult`, `Runner`, and `subprocess_runner` from `reviewer.install_codex`.
- Produces `ClaudeInstallOptions`, `ClaudeInstallResult`, `ClaudeInstallError`, `find_claude_executable()`, `detect_claude_capabilities()`, and `run_claude_install()`.

- [ ] **Step 1: Write failing lifecycle tests**

Implement a file-backed fake that returns the actual array JSON shapes from `claude plugin marketplace list --json` and `claude plugin list --json`. Test fresh install, repeat install, incorrect/non-HTTPS marketplace rejection, disabled or foreign plugin rejection, failed marketplace/plugin commands, dry-run without mutation, and a missing executable.

```python
assert fake.calls[-2:] == [
    (str(fake.executable), "plugin", "marketplace", "add",
     "https://github.com/mimfort/rag_for_git.git", "--scope", "user",
     "--sparse", ".claude-plugin", "plugin"),
    (str(fake.executable), "plugin", "install",
     "rag-reviewer@rag-reviewer-marketplace", "--scope", "user"),
]
assert result.plugin.enabled is True
```

Add a payload test for the portable plugin MCP configuration:

```python
assert json.loads((ROOT / "plugin/.mcp.json").read_text())["mcpServers"]["reviewer"] == {
    "command": "uvx",
    "args": ["--from", "rag-reviewer@latest", "reviewer-mcp"],
}
```

- [ ] **Step 2: Verify RED**

Run `uv run pytest -q tests/install/test_claude_install.py`.

Expected: collection fails because the adapter and fake do not exist.

- [ ] **Step 3: Implement the minimal adapter**

Use these constants:

```python
CLAUDE_MARKETPLACE_NAME = "rag-reviewer-marketplace"
CLAUDE_PLUGIN_ID = "rag-reviewer@rag-reviewer-marketplace"
CLAUDE_MARKETPLACE_SOURCE = "https://github.com/mimfort/rag_for_git.git"
CLAUDE_SPARSE = (".claude-plugin", "plugin")
```

Feature-detect `plugin marketplace add/list` and `plugin install/list` help. On a real run execute marketplace add, then plugin install, then require exact HTTPS marketplace ownership plus installed/enabled user-scope plugin from fresh JSON. Raise `ClaudeInstallError(phase, argv, detail)` for any command or verification error. On dry-run return the intended argv without mutation. Change `plugin/.mcp.json` to the direct `uvx` entry asserted above.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest -q tests/install/test_claude_install.py tests/install/test_codex_plugin_payload.py
uv run ruff check reviewer/install_claude.py tests/install/fake_claude.py tests/install/test_claude_install.py
```

- [ ] **Step 5: Commit Task 1**

```bash
git add reviewer/install_claude.py tests/install/fake_claude.py tests/install/test_claude_install.py plugin/.mcp.json
git commit -m "feat(install): add Claude Code plugin lifecycle"
```

### Task 2: Route native Claude Code through `install` and `--all`

**Files:**

- Modify: `reviewer/entrypoints/cli.py:48-101,347-450`
- Modify: `reviewer/install.py:470-510`
- Create: `tests/install/test_claude_cli.py`
- Modify: `tests/install/test_codex_cli.py`

**Interfaces:**

- Consumes `run_claude_install(ClaudeInstallOptions)` from Task 1 and `build_allowlist_plan()` / `apply_allowlist_plan()` from `reviewer.install`.
- Produces `_run_claude_target()`, `_print_claude_result()`, and native Claude target selection.

- [ ] **Step 1: Write failing dispatch tests**

Monkeypatch the adapter and assert explicit `install claude-code` invokes it, prints Claude plugin status, updates only global allowlist, and does not create `.mcp.json` in the current project. Assert `install --all` includes Claude only when `_shutil.which("claude")` succeeds, continues Cursor/Codex after a Claude failure, and returns an aggregated `Claude Code CLI:` error. Add tests for `--dry-run`, `--path` rejection, and `--no-skills` MCP-only routing.

```python
result = CliRunner().invoke(cli, ["install", "claude-code"])
assert result.exit_code == 0, result.output
assert captured[0].dry_run is False
assert not Path(".mcp.json").exists()
```

- [ ] **Step 2: Verify RED**

Run `uv run pytest -q tests/install/test_claude_cli.py tests/install/test_codex_cli.py`.

Expected: the new Claude tests fail because the generic branch writes project `.mcp.json` and `--all` excludes Claude.

- [ ] **Step 3: Implement native routing**

Add helpers alongside `_run_codex_target()`. In normal mode invoke Task 1 then update the global allowlist. Keep Claude Code non-generic in `CLIENTS` so generic detection never treats the current directory as an installed client. In `--all`, append Claude only when the public executable exists.

For `--no-skills`, use `claude mcp get reviewer`; accept an existing canonical user-scope `uvx` entry, otherwise replace an existing noncanonical user entry with documented `claude mcp remove reviewer --scope user` then `claude mcp add --scope user reviewer -- uvx --from rag-reviewer@<version> reviewer-mcp`. Verify its documented public text output and update the global allowlist. Reject `--path` for native Claude mode. Accumulate Claude errors after other targets exactly as Codex errors are accumulated. Reject a non-default `--pin` or `--no-latest` in plugin mode rather than silently ignoring the static plugin manifest version; MCP-only mode continues to use `launch_command()`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest -q tests/install/test_claude_cli.py tests/install/test_codex_cli.py tests/install/test_install.py
uv run ruff check reviewer/entrypoints/cli.py reviewer/install.py tests/install/test_claude_cli.py
```

- [ ] **Step 5: Commit Task 2**

```bash
git add reviewer/entrypoints/cli.py reviewer/install.py tests/install/test_claude_cli.py tests/install/test_codex_cli.py tests/install/test_install.py
git commit -m "feat(install): include global Claude lifecycle in install all"
```

### Task 3: Repair the repository-wide Ruff baseline

**Files:**

- Modify: `reviewer/graph/scip.py`
- Modify: `reviewer/vcs/diff.py`
- Modify: `tests/graph/test_scip.py`
- Modify: `tests/index/test_schema.py`
- Modify: `tests/index/test_store_hybrid.py`
- Modify: `tests/integration/test_pipeline.py`

- [ ] **Step 1: Capture the failing baseline**

Run `uv run ruff check .` and preserve the list of 11 known violations as the
test-first failure evidence.

- [ ] **Step 2: Make behavior-preserving repairs**

Split multi-imports and semicolon-separated statements, and remove the unused
test import. Do not change runtime or test assertions beyond those mechanical
Ruff repairs.

- [ ] **Step 3: Verify the repaired baseline**

Run:

```bash
uv run pytest -q tests/graph/test_scip.py tests/index/test_schema.py
uv run ruff check .
git diff --check
```

- [ ] **Step 4: Commit Task 3**

```bash
git add reviewer/graph/scip.py reviewer/vcs/diff.py tests/graph/test_scip.py tests/index/test_schema.py tests/index/test_store_hybrid.py tests/integration/test_pipeline.py
git commit -m "style: repair Ruff baseline"
```

### Task 4: Registry-wide generic coverage and public docs

**Files:**

- Modify: `tests/install/test_install.py`
- Modify: `tests/install/test_install_skills_cli.py` if fixture coverage needs it
- Modify: `README.md:240-265,290-405`
- Modify: matching install sections in `README.ru.md`
- Modify: `AGENTS.md` if Claude global verification needs a short companion section

**Interfaces:**

- Consumes `CLIENTS`, `build_plan()`, `install_skills()`, and Tasks 1–2 dispatch.
- Produces a regression guard for every generic client configuration.

- [ ] **Step 1: Write failing all-client registry test**

Create every generic client config parent beneath a temporary home, monkeypatch `_home()` and `detect_installed()`, and invoke `install --all` with native targets stubbed. Parse each output and assert exactly one reviewer entry, correct dialect, and preservation of unrelated fixture data. Invoke twice to prove idempotency. Use a synthetic skills tarball and assert Gemini, Mimo, OpenCode, and Kimi get their expected global skill destinations.

```python
assert json.loads(cursor_path.read_text())["mcpServers"]["reviewer"]["args"][-1] == "reviewer-mcp"
assert json.loads(vscode_path.read_text())["servers"]["reviewer"]["command"] == fake_uvx
assert json.loads(mimo_path.read_text())["mcp"]["reviewer"]["enabled"] is True
assert json.loads(opencode_path.read_text())["mcp"]["reviewer"]["type"] == "local"
```

- [ ] **Step 2: Verify RED**

Run `uv run pytest -q tests/install/test_install.py -k all_clients_registry`.

Expected: test fails until fixture/native routing assumptions are wired correctly.

- [ ] **Step 3: Implement only required generic wiring and docs**

Do not add plugin lifecycle code for config-only applications. Keep format generation in `build_plan()`. Update English and Russian docs to state that explicit/detected Claude Code installs the user-scope plugin from HTTPS, explain `--no-skills`, show `claude plugin list --json` verification, and retain restart guidance.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest -q tests/install/test_install.py tests/install/test_install_skills_cli.py tests/install/test_claude_install.py tests/install/test_claude_cli.py
uv run ruff check .
git diff --check
```

- [ ] **Step 5: Commit Task 4**

```bash
git add tests/install/test_install.py tests/install/test_install_skills_cli.py README.md README.ru.md AGENTS.md
git commit -m "test(install): cover all supported client configurations"
```

### Task 5: End-to-end acceptance and PR handoff

**Files:**

- Modify only a test or report if verification proves a missing regression; otherwise no production edits.

- [ ] **Step 1: Test actual CLIs in isolated profiles**

From an outside-repository CWD run:

```bash
HOME=$(mktemp -d /private/tmp/reviewer-all-home.XXXXXX) \
CODEX_HOME=$(mktemp -d /private/tmp/reviewer-codex-home.XXXXXX) \
CLAUDE_CONFIG_DIR=$(mktemp -d /private/tmp/reviewer-claude-config.XXXXXX) \
uv run --project "$WORKTREE" reviewer install --all
```

Verify `codex plugin list --json`, `codex mcp get reviewer`, `claude plugin marketplace list --json`, and `claude plugin list --json`; repeat once for idempotency. Validate the branch-local `plugin/.mcp.json` directly as part of Task 1: a remote marketplace install before merge necessarily receives the repository's current remote manifest, not this unmerged branch. Do not touch a live profile.

- [ ] **Step 2: Run final verification**

Run:

```bash
uv sync --extra dev --extra web
uv run pytest -q
uv run ruff check .
git diff --check
```

Record exact counts. The repaired repository-wide lint baseline must remain clean.

- [ ] **Step 3: Final review and PR**

Request a read-only code review against the design and this plan. Fix every Critical or Important finding, repeat Step 2, then push and create the requested PR:

```bash
git push -u origin fix/codex-marketplace-contract
gh pr create --base dev --head fix/codex-marketplace-contract \
  --title "fix(install): make global client setup reliable" \
  --body-file /private/tmp/rag-reviewer-pr-body.md
```

The PR description must include the Codex metadata/MCP fix, Claude lifecycle, registry coverage, isolated CLI evidence, and exact test results.
