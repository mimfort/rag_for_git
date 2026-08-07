# Installing rag-reviewer for Kimi Code

## Prerequisites

- [Kimi Code](https://kimi.moonshot.cn) installed
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Быстрая установка (рекомендуется)

```bash
uvx --from rag-reviewer reviewer install kimi
```

Пропишет MCP-сервер в `~/.kimi-code/mcp.json` автоматически (кроссплатформенно,
подставляет абсолютный путь к `uvx` — обёртка `bash -lc` не нужна) **и установит скилы**.

## Ручная установка (альтернатива)

Создайте `~/.kimi-code/mcp.json` (или добавьте в существующий):

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

Перезапустите Kimi Code. MCP-сервер `reviewer` будет доступен во всех сессиях.

## Project-level install

Copy `.kimi-code/mcp.json` from this repo into your target project. The config is
already present here (see `mcp.json` in this directory).

## Skills (optional)

Kimi Code loads skills from directories listed in `extra_skill_dirs` in `~/.kimi-code/config.toml`.

**Step 1** — add the skills directory to your config:

```toml
# ~/.kimi-code/config.toml
extra_skill_dirs = ["~/.kimi-code/skills"]
```

**Step 2** — install the skills:

Рекомендуемый способ — через основную команду установки (если уже выполнена выше, скилы
уже стоят):

```bash
uvx --from rag-reviewer reviewer install kimi      # MCP + скилы
# или только скилы:
uvx --from rag-reviewer reviewer install-skills kimi
```

**Офлайн-альтернатива** (снапшот; не обновляется сам — для обновления используйте
`reviewer install kimi`, см. раздел «Обновление»):

```bash
curl -sL https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz -o /tmp/rag-reviewer.tgz
mkdir -p ~/.kimi-code/skills
tar xz -C ~/.kimi-code/skills --strip-components=3 -f /tmp/rag-reviewer.tgz 'rag_for_git-main/plugin/skills'
rm /tmp/rag-reviewer.tgz
```

Skills installed: `review-pr`, `solve-task`, `sync-codebase`, `sync-tasks`,
`performance-review`, `maintainability-review`.

## Обновление

Скилы — это снапшот, скачанный при установке; сами они не обновляются. После апгрейда
сервера освежите их повторным запуском:

```bash
uvx --from rag-reviewer reviewer install kimi      # MCP + свежие скилы
# или только скилы:
uvx --from rag-reviewer reviewer install-skills kimi
```

`reviewer check` предупредит, если установленные скилы устарели.

## Verify

Open Kimi Code and run:
> "List available MCP tools"

You should see `prepare_review`, `publish_review`, `search_code`, etc.

## Usage

See `plugin/skills/review-pr/SKILL.md` for the full review workflow.
