"""Кроссплатформенная установка MCP-сервера reviewer в разные AI-CLI/IDE.

Одна команда `reviewer install <client>` пишет корректную MCP-запись в конфиг
нужного клиента (Cursor, VS Code, Antigravity, Claude Desktop/Code, Gemini,
Codex, Mimo, OpenCode, Kimi, Windsurf, Trae) — с учётом ОС и диалекта конфига.

Ключевые решения для удобства и кроссплатформенности:
- команда запуска использует **абсолютный путь** к `uvx`/`uv` (а не `bash -lc`),
  что решает проблему «GUI не видит PATH» сразу на Windows/macOS/Linux;
- по умолчанию пин `rag-reviewer@latest` — автообновление при каждом старте;
- перед записью делается `.bak`, чужие записи в конфиге сохраняются (merge).
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import tomllib
from dataclasses import dataclass, field as _field
from pathlib import Path, PureWindowsPath
from typing import Callable
from urllib.parse import urlsplit

from reviewer.config.provider_access import ProviderAccessSpec
from reviewer.tasks.boards.registry import default_board_registry as _default_board_registry

PACKAGE = "rag-reviewer"
SERVER_NAME = "reviewer"
COMMON_BOARD_ENV_KEYS = frozenset(
    {
        "TASK_BOARD_KEY_PATTERN",
        "TASK_BOARD_URL_TEMPLATE",
    }
)

# anchored-glob правило, разрешающее ВЕСЬ сервер reviewer в Claude Code
# (permissions.allow). Wildcard покрывает новые тулы автоматически — список
# тулов синхронизировать не нужно.
REVIEWER_PERMISSION_RULE = "mcp__reviewer__*"

# Скилы лежат в репозитории (plugin/skills) и в wheel не пакуются — тянем их
# тарболом с GitHub (кроссплатформенно, без клона).
SKILLS_TARBALL = "https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz"

# Шаблон .env (встроен, чтобы `reviewer init` работал из published-wheel без
# упаковки файла). Все остальные переменные имеют дефолты в settings.py.
_ENV_TEMPLATE_BASE = """\
# rag_for_git — конфигурация. Обязателен только VOYAGE_API_KEY; GITHUB_TOKEN
# нужен для ревью PR. Остальное имеет дефолты в reviewer/config/settings.py.
# Полный справочник полей — зеркало .env.example.

# --- Voyage (эмбеддинги + реранкер) — обязательно ---
VOYAGE_API_KEY=
EMBEDDING_MODEL=voyage-code-3
EMBEDDING_DIM=1024
EMBEDDING_BATCH_SIZE=256
EMBEDDING_TOKEN_BUDGET=100000
RERANK_MODEL=rerank-2.5

# --- GitHub (PAT: Pull requests read/write, Contents read) ---
GITHUB_TOKEN=
GITHUB_RETRY_ATTEMPTS=3
GITHUB_RETRY_BACKOFF_BASE=1.0

# --- multi-platform VCS: тип определяется из git remote при `reviewer index` ---
VCS_PROVIDER=github
GITLAB_TOKEN=
GITLAB_URL=https://gitlab.com

# --- Postgres (ParadeDB :5433) / Neo4j (:7687) — дефолты docker-compose ---
PG_DSN=postgresql://reviewer:reviewer@localhost:5433/reviewer
PG_POOL_MIN_SIZE=1
PG_POOL_MAX_SIZE=4
NEO4J_URI=neo4j://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=reviewerpass

# --- Граф кода: auto|scip|treesitter ---
GRAPH_BACKEND=auto
SUMMARY_CLUSTER_DEPTH=2

# --- Мульти-репо / ветки (опционально) ---
DEFAULT_REPO=
REVIEW_BRANCHES=main,master

# --- Настройка ревью (дефолты; per-repo .review.yml может переопределить) ---
REVIEW_SEVERITY_THRESHOLD=medium
REVIEW_MIN_CONFIDENCE=0.5
REVIEW_MAX_COMMENTS=25
REVIEW_MAX_FILES=50
REVIEW_CATEGORIES=
REVIEW_SUGGESTIONS=apply
REVIEW_OUTPUT_LANGUAGE=ru
REVIEW_SKIP_DRAFTS=true
REVIEW_HISTORY=true
MAX_TOOL_RESULT_CHARS=8000
REVIEW_BUG_REPORTS=true

# --- Доска задач (опционально; canonical per-provider credentials) ---
# Тип доски (yougile|youtrack|jira) задаётся в .review.yml каждого репо (task_board.type),
# а не в env; TASK_BOARD_TYPE устарел и игнорируется.
# Legacy TASK_BOARD_API_KEY/BASE только читаются как YouGile compatibility aliases:
# новые конфиги и preview их не генерируют.
# Jira: https://id.atlassian.com/manage-profile/security/api-tokens
# YouTrack: Profile → Account Security → permanent token (YouTrack scope, полный perm:).
# YouGile: https://ru.yougile.com/api-v2; reviewer init умеет получить key автоматически.
TASK_BOARD_MCP=
TASK_BOARD_KEY_PATTERN=
TASK_BOARD_URL_TEMPLATE=

# --- Веб-админка наблюдаемости (опционально; пусто = без аутентификации) ---
WEB_ADMIN_USER=
WEB_ADMIN_PASSWORD=
"""


@dataclass
class EnvField:
    key: str
    prompt_text: str
    default: str = ""
    secret: bool = False
    required: bool = False
    # Дефолт, выводимый из уже собранных значений (напр. порт из PG_DSN).
    # None — статичный default.
    derive_default: Callable[[dict[str, str]], str] | None = None


@dataclass
class EnvGroup:
    title: str
    fields: list[EnvField] = _field(default_factory=list)
    optional: bool = False


@dataclass(frozen=True)
class VcsSetupSpec:
    """Несекретный setup-контракт одного VCS provider."""

    provider: str
    label: str
    credential_fields: tuple[EnvField, ...]
    help_url: str
    help_text: str
    access: ProviderAccessSpec


VCS_SETUPS = {
    "github": VcsSetupSpec(
        provider="github",
        label="GitHub",
        credential_fields=(
            EnvField("GITHUB_TOKEN", "GitHub personal access token", secret=True),
        ),
        help_url="https://github.com/settings/personal-access-tokens/new",
        help_text="Создайте fine-grained PAT для репозитория reviewer.",
        access=ProviderAccessSpec(
            minimum_permissions="Pull requests: Read and write; Contents: Read",
            read_operations=("PR metadata, files, comments, contents and compare",),
            write_operations=("review comments/summary and PR body backlink",),
            validation="reviewer check authenticates /user identity",
        ),
    ),
    "gitlab": VcsSetupSpec(
        provider="gitlab",
        label="GitLab",
        credential_fields=(
            EnvField("GITLAB_URL", "GitLab base URL", default="https://gitlab.com"),
            EnvField(
                "GITLAB_TOKEN",
                "GitLab personal/project access token",
                secret=True,
            ),
        ),
        help_url="https://docs.gitlab.com/user/profile/personal_access_tokens/",
        help_text="Создайте PAT или project access token для GitLab API v4.",
        access=ProviderAccessSpec(
            minimum_permissions="PAT/project token with api scope",
            read_operations=("MR metadata, changes, notes, repository files and compare",),
            write_operations=("MR discussions/notes and description backlink",),
            validation="reviewer check authenticates /api/v4/user identity",
        ),
    ),
}


def vcs_env_group() -> EnvGroup:
    """Canonical union VCS-полей для render/preserve, не prompt selection."""
    fields = [EnvField("VCS_PROVIDER", "VCS fallback provider", default="github")]
    for spec in VCS_SETUPS.values():
        fields.extend(spec.credential_fields)
    return EnvGroup(title="VCS", fields=fields, optional=True)


def board_env_group(registry) -> EnvGroup:
    """Построить canonical env-поля досок из registry metadata.

    Compatibility aliases читаются credential source, но новый конфиг их не
    генерирует.
    """
    fields = [
        EnvField(
            key="TASK_BOARD_KEY_PATTERN",
            prompt_text=r"TASK_BOARD_KEY_PATTERN (напр. [A-Z]+-\d+)",
        ),
        EnvField(
            key="TASK_BOARD_URL_TEMPLATE",
            prompt_text="TASK_BOARD_URL_TEMPLATE (напр. https://.../{code})",
        ),
    ]
    for board_type in registry.registered_types():
        spec = registry.get(board_type)
        fields.extend(
            EnvField(
                key=credential.env,
                prompt_text=credential.label,
                default=credential.default,
                secret=credential.secret,
                required=credential.required,
            )
            for credential in spec.credential_fields
        )
    return EnvGroup(title="Доска задач", fields=fields, optional=True)


def common_board_env_fields(group: EnvGroup) -> list[EnvField]:
    """Только два non-secret поля общей связки; prefix matching запрещён."""
    return [field for field in group.fields if field.key in COMMON_BOARD_ENV_KEYS]


def _env_template_with_board_fields(template: str, group: EnvGroup) -> str:
    marker = "TASK_BOARD_URL_TEMPLATE=\n"
    provider_lines = "\n".join(
        f"{field.key}={field.default}"
        for field in group.fields
        if field.key not in COMMON_BOARD_ENV_KEYS
    )
    return template.replace(marker, f"{marker}{provider_lines}\n", 1)


WIZARD_GROUPS: list[EnvGroup] = [
    EnvGroup(
        title="Обязательные",
        optional=False,
        fields=[
            EnvField(
                key="VOYAGE_API_KEY",
                prompt_text="VOYAGE_API_KEY (эмбеддинги + реранкер)",
                secret=True,
                required=True,
            ),
        ],
    ),
    EnvGroup(
        title="Хранилища (Postgres / Neo4j)",
        optional=True,
        fields=[
            EnvField(
                key="PG_DSN",
                prompt_text="PG_DSN",
                default="postgresql://reviewer:reviewer@localhost:5433/reviewer",
            ),
            EnvField(key="NEO4J_URI", prompt_text="NEO4J_URI", default="neo4j://localhost:7687"),
            EnvField(key="NEO4J_USER", prompt_text="NEO4J_USER", default="neo4j"),
            EnvField(
                key="NEO4J_PASSWORD",
                prompt_text="NEO4J_PASSWORD",
                default="reviewerpass",
                secret=True,
            ),
        ],
    ),
    vcs_env_group(),
    board_env_group(_default_board_registry()),
]

ENV_TEMPLATE = _env_template_with_board_fields(
    _ENV_TEMPLATE_BASE,
    next(group for group in WIZARD_GROUPS if group.title == "Доска задач"),
)


def read_env(path: Path) -> dict[str, str]:
    """Прочитать KEY=VALUE из .env, пропуская комментарии и пустые строки."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


_GROUP_HEADERS: dict[str, str] = {
    "Обязательные": "# --- Voyage ---",
    "Хранилища (Postgres / Neo4j)": "# --- Postgres (ParadeDB :5433) / Neo4j (:7687) ---",
    "VCS": (
        "# --- VCS credentials (GitHub / GitLab) ---\n"
        "# VCS_PROVIDER — фолбэк, когда репо не индексирован. Тип VCS\n"
        "# автоопределяется из git remote при `reviewer index`."
    ),
    "Доска задач": (
        "# --- Доска задач (опционально; server-side sync_board, связка ключей) ---\n"
        "# Тип доски репо выбирается в его .review.yml (task_board.type); ключи —\n"
        "# здесь, под каждую доску свой. Jira требует direct Cloud site URL и API token\n"
        "# без scopes. YouTrack permanent token хранится целиком с perm: и service scope.\n"
        "# YouGile key можно получить автоматически через reviewer init; password не пишется.\n"
        "# TASK_BOARD_API_KEY/BASE — только read-only compatibility aliases для YouGile."
    ),
}


def render_env(values: dict[str, str], extra: dict[str, str]) -> str:
    """Сгенерировать содержимое .env по wizard-группам и прочим (extra) ключам."""
    lines: list[str] = [
        "# rag_for_git — конфигурация (сгенерировано reviewer init)",
        "# Обязательный ключ: VOYAGE_API_KEY; выбранный VCS token нужен для ревью PR.",
        "# Остальные переменные имеют дефолты в reviewer/config/settings.py.",
        "",
    ]
    for group in WIZARD_GROUPS:
        header = _GROUP_HEADERS.get(group.title, f"# --- {group.title} ---")
        lines.append(header)
        for field in group.fields:
            lines.append(f"{field.key}={values.get(field.key, field.default)}")
        lines.append("")

    if extra:
        lines.append("# Прочие настройки")
        for key, value in extra.items():
            lines.append(f"{key}={value}")
        lines.append("")

    return "\n".join(lines)


def render_env_preview(values: dict[str, str], extra: dict[str, str]) -> str:
    """Безопасный preview: показывает secret keys, но скрывает их значения."""
    secret_keys = {
        field.key
        for group in WIZARD_GROUPS
        for field in group.fields
        if field.secret
    }
    registry = _default_board_registry()
    secret_keys.update(
        alias
        for board_type in registry.registered_types()
        for field in registry.get(board_type).credential_fields
        if field.secret
        for alias in field.aliases
    )

    def is_secret_key(key: str) -> bool:
        upper = key.upper()
        return key in secret_keys or any(
            marker in upper
            for marker in ("TOKEN", "PASSWORD", "API_KEY", "SECRET", "PRIVATE_KEY")
        )

    def safe_value(key: str, value: str) -> str:
        if is_secret_key(key):
            return ""
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if parsed.scheme and (parsed.username is not None or parsed.password is not None):
            return ""
        return value

    safe_values = {
        key: safe_value(key, value)
        for key, value in values.items()
    }
    safe_extra = {
        key: safe_value(key, value)
        for key, value in extra.items()
    }
    return render_env(safe_values, safe_extra)


def _port_from_url(value: str, fallback: str) -> str:
    """Порт из URL (DSN/URI) строкой; fallback при пустом/кривом URL или URL без порта."""
    try:
        port = urlsplit((value or "").strip()).port
    except ValueError:
        return fallback
    return str(port) if port else fallback


def _effective_default(
    field: EnvField,
    values: dict[str, str],
    current: dict[str, str],
) -> str:
    """Дефолт поля: значение из .env → производный (derive_default) → статичный."""
    cur = current.get(field.key, "")
    if cur:
        return cur
    if field.derive_default is not None:
        return field.derive_default(values)
    return field.default


def prompt_groups(
    groups: list[EnvGroup],
    current: dict[str, str],
    yes: bool,
) -> dict[str, str]:
    """Интерактивно запросить значения полей по группам.

    yes=True — CI-режим: без prompt'ов, берём current или field.default.
    Секрет с существующим значением: показываем «уже задан», пустой ввод = оставить.
    """
    import click

    values: dict[str, str] = {}

    for group in groups:
        # Опциональную группу предваряем вопросом (в интерактивном режиме)
        if group.optional:
            if yes:
                # CI: сохраняем текущее или дефолт, не спрашиваем
                for f in group.fields:
                    values[f.key] = _effective_default(f, values, current)
                continue
            if not click.confirm(f"\nНастроить {group.title}?", default=False):
                for f in group.fields:
                    values[f.key] = _effective_default(f, values, current)
                continue
        elif not yes:
            click.echo(f"\n[{group.title}]")

        for field in group.fields:
            cur = current.get(field.key, "")
            effective_default = _effective_default(field, values, current)

            if yes:
                values[field.key] = effective_default
                continue

            if field.secret:
                if cur:
                    label = f"{field.prompt_text} (уже задан — Enter чтобы оставить)"
                    val = click.prompt(label, default="", hide_input=True, show_default=False)
                    values[field.key] = val if val else cur
                else:
                    val = click.prompt(field.prompt_text, default="", hide_input=True,
                                       show_default=False)
                    values[field.key] = val
            else:
                values[field.key] = click.prompt(field.prompt_text, default=effective_default)

    return values


def default_env_path() -> Path:
    """Каноническое место .env: $XDG_CONFIG_HOME/rag-reviewer/.env."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(xdg) / "rag-reviewer" / ".env"


def claude_settings_path() -> Path:
    """Проектный settings.json Claude Code (рядом с .mcp.json в корне проекта)."""
    return Path(".claude") / "settings.json"


def claude_user_settings_path() -> Path:
    """Глобальный (user-scope) settings.json Claude Code: ~/.claude/settings.json.

    Allowlist-правило reviewer пишем сюда, а не в проект: тогда оно действует во
    ВСЕХ проектах сразу — и при проектной установке (.mcp.json в CWD), и при
    установке плагином через marketplace (сервер глобален, но плагин не раздаёт
    разрешений). Где reviewer не подключён — правило инертно (сервера нет).
    """
    return _home() / ".claude" / "settings.json"


# --------------------------------------------------------------------------- #
# команда запуска
# --------------------------------------------------------------------------- #
def _absolute_launcher_path(found: str) -> str:
    path = Path(found)
    if path.is_absolute() or PureWindowsPath(found).is_absolute():
        return found
    return str(path.resolve())


def launch_command(version: str = "latest") -> tuple[str, list[str]]:
    """(command, args) для запуска reviewer-mcp.

    version="latest" → `rag-reviewer@latest` (автообновление);
    version=""       → `rag-reviewer` (без пина, берётся из кэша);
    version="0.1.2"  → конкретный пин.
    Абсолютный путь к uvx/uv устраняет зависимость от PATH в GUI-клиентах.
    """
    spec = f"{PACKAGE}@{version}" if version else PACKAGE
    tail = ["--from", spec, "reviewer-mcp"]
    uvx = shutil.which("uvx")
    if uvx:
        return _absolute_launcher_path(uvx), tail
    uv = shutil.which("uv")
    if uv:
        return _absolute_launcher_path(uv), ["tool", "run", *tail]
    raise RuntimeError(
        "uvx/uv не найден; установите uv и повторите reviewer install"
    )


# --------------------------------------------------------------------------- #
# реестр клиентов
# --------------------------------------------------------------------------- #
def _home() -> Path:
    return Path.home()


def codex_home_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else _home() / ".codex"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming"))


def _vscode_path(system: str) -> Path:
    if system == "Darwin":
        base = _home() / "Library" / "Application Support" / "Code" / "User"
    elif system == "Windows":
        base = _appdata() / "Code" / "User"
    else:
        base = _home() / ".config" / "Code" / "User"
    return base / "mcp.json"


def _claude_desktop_path(system: str) -> Path:
    name = "claude_desktop_config.json"
    if system == "Darwin":
        return _home() / "Library" / "Application Support" / "Claude" / name
    if system == "Windows":
        return _appdata() / "Claude" / name
    return _home() / ".config" / "Claude" / name


def _trae_path(system: str) -> Path:
    if system == "Darwin":
        return _home() / "Library" / "Application Support" / "Trae" / "User" / "mcp.json"
    if system == "Windows":
        return _appdata() / "Trae" / "User" / "mcp.json"
    return _home() / ".config" / "Trae" / "User" / "mcp.json"


@dataclass(frozen=True)
class Client:
    key: str
    label: str
    dialect: str  # mcpServers | vscode | mimo | opencode | codex | native
    path_fn: Callable[[str], Path]
    scope: str = "user"  # user | project | native
    note: str = ""
    # каталог глобальных скилов клиента (None — файловые скилы не поддерживаются)
    skills_fn: Callable[[str], Path] | None = None


CLIENTS: dict[str, Client] = {
    c.key: c
    for c in [
        Client("cursor", "Cursor", "mcpServers", lambda s: _home() / ".cursor" / "mcp.json"),
        Client("claude-desktop", "Claude Desktop", "mcpServers", _claude_desktop_path),
        Client("claude-code", "Claude Code", "native",
               lambda s: claude_user_settings_path(), scope="native",
               note="глобальный plugin marketplace через Claude Code CLI"),
        Client("vscode", "VS Code", "vscode", _vscode_path),
        Client("windsurf", "Windsurf", "mcpServers",
               lambda s: _home() / ".codeium" / "windsurf" / "mcp_config.json"),
        Client("gemini", "Gemini CLI", "mcpServers",
               lambda s: _home() / ".gemini" / "settings.json",
               skills_fn=lambda s: _home() / ".gemini" / "skills"),
        Client("antigravity", "Antigravity", "mcpServers",
               lambda s: _home() / ".gemini" / "antigravity" / "mcp_config.json"),
        Client("mimo", "Mimo Code", "mimo",
               lambda s: _home() / ".config" / "mimocode" / "mimocode.json",
               skills_fn=lambda s: _home() / ".config" / "mimocode" / "skills"),
        Client("opencode", "OpenCode", "opencode",
               lambda s: _home() / ".config" / "opencode" / "opencode.json",
               skills_fn=lambda s: _home() / ".config" / "opencode" / "skills"),
        Client("kimi", "Kimi Code", "mcpServers",
               lambda s: _home() / ".kimi-code" / "mcp.json",
               skills_fn=lambda s: _home() / ".kimi-code" / "skills",
               note="для скилов добавьте extra_skill_dirs=[\"~/.kimi-code/skills\"] "
                    "в ~/.kimi-code/config.toml"),
        Client("trae", "Trae IDE", "mcpServers", _trae_path),
        Client("codex", "Codex CLI", "codex",
               lambda s: codex_home_path() / "config.toml"),
    ]
}

# ключ верхнего уровня для JSON-диалектов
_TOP_KEY = {"mcpServers": "mcpServers", "vscode": "servers", "mimo": "mcp", "opencode": "mcp"}


def _entry(dialect: str, command: str, args: list[str]) -> dict:
    """MCP-запись reviewer в формате конкретного диалекта."""
    if dialect in ("mcpServers", "vscode"):
        return {"command": command, "args": args}
    if dialect == "mimo":
        return {"type": "local", "command": [command, *args], "enabled": True}
    if dialect == "opencode":
        return {"type": "local", "command": [command, *args]}
    raise ValueError(f"неизвестный диалект: {dialect}")


def _toml_string(value: str) -> str:
    escapes = {
        '"': r'\"',
        "\\": r"\\",
        "\b": r"\b",
        "\t": r"\t",
        "\n": r"\n",
        "\f": r"\f",
        "\r": r"\r",
    }
    encoded: list[str] = []
    for char in value:
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("TOML-строка содержит недопустимый Unicode surrogate")
        if char in escapes:
            encoded.append(escapes[char])
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            encoded.append(f"\\u{codepoint:04X}")
        else:
            encoded.append(char)
    return f'"{"".join(encoded)}"'


def _render_codex(command: str, args: list[str]) -> str:
    args_toml = ", ".join(_toml_string(a) for a in args)
    return (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f"command = {_toml_string(command)}\n"
        f"args = [{args_toml}]\n"
    )


def _toml_key_path(source: str) -> tuple[str, ...] | None:
    try:
        parsed = tomllib.loads(f"{source} = 0\n")
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    node: object = parsed
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
    return tuple(path) if path and node == 0 else None


def _toml_table_header(line: str) -> tuple[tuple[str, ...], bool] | None:
    text = line.rstrip("\r\n")
    index = len(text) - len(text.lstrip(" \t"))
    array = text.startswith("[[", index)
    opener = 2 if array else 1
    if not text.startswith("[" * opener, index):
        return None
    index += opener
    start = index
    quote: str | None = None
    escaped = False
    closer = "]" * opener
    while index < len(text):
        char = text[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif quote == "'":
            if char == "'":
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif text.startswith(closer, index):
            path = _toml_key_path(text[start:index])
            remainder = text[index + opener:].lstrip(" \t")
            if path is not None and (not remainder or remainder.startswith("#")):
                return path, array
            return None
        index += 1
    return None


def _toml_lex_state(
    line: str,
    multiline: str | None,
    array_depth: int,
) -> tuple[str | None, int]:
    index = 0
    quote: str | None = None
    while index < len(line):
        if multiline == '"""':
            if line[index] == "\\":
                index += 2
            elif line.startswith(multiline, index):
                multiline = None
                while index < len(line) and line[index] == '"':
                    index += 1
            else:
                index += 1
        elif multiline == "'''":
            if line.startswith(multiline, index):
                multiline = None
                while index < len(line) and line[index] == "'":
                    index += 1
            else:
                index += 1
        elif quote == '"':
            if line[index] == "\\":
                index += 2
            elif line[index] == '"':
                quote = None
                index += 1
            else:
                index += 1
        elif quote == "'":
            if line[index] == "'":
                quote = None
            index += 1
        elif line[index] == "#":
            break
        elif line.startswith('"""', index):
            multiline = '"""'
            index += 3
        elif line.startswith("'''", index):
            multiline = "'''"
            index += 3
        elif line[index] in ('"', "'"):
            quote = line[index]
            index += 1
        elif line[index] == "[":
            array_depth += 1
            index += 1
        elif line[index] == "]":
            array_depth -= 1
            index += 1
        else:
            index += 1
    return multiline, array_depth


def _toml_table_headers(
    lines: list[str],
) -> dict[int, tuple[tuple[str, ...], bool]]:
    headers: dict[int, tuple[tuple[str, ...], bool]] = {}
    multiline: str | None = None
    array_depth = 0
    for index, line in enumerate(lines):
        if multiline is None and array_depth == 0:
            header = _toml_table_header(line)
            if header is not None:
                headers[index] = header
        multiline, array_depth = _toml_lex_state(line, multiline, array_depth)
    return headers


def _toml_source_lines(raw: str) -> list[str]:
    chunks = raw.split("\n")
    lines = [chunk + "\n" for chunk in chunks[:-1]]
    if chunks[-1]:
        lines.append(chunks[-1])
    return lines


def _toml_table_name(line: str) -> str | None:
    header = _toml_table_header(line)
    return ".".join(header[0]) if header is not None else None


def _is_reviewer_table(name: str) -> bool:
    return name == "mcp_servers.reviewer" or name.startswith("mcp_servers.reviewer.")


def _is_reviewer_path(path: tuple[str, ...]) -> bool:
    return len(path) >= 2 and path[:2] == ("mcp_servers", "reviewer")


def _replace_codex_reviewer_tables(raw: str, rendered: str) -> tuple[str, bool]:
    try:
        parsed = tomllib.loads(raw) if raw.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"невалидный TOML: {exc}") from exc
    lines = _toml_source_lines(raw)
    headers = _toml_table_headers(lines)
    output: list[str] = []
    insert_at: int | None = None
    removed = False
    index = 0
    while index < len(lines):
        header = headers.get(index)
        if header is not None and _is_reviewer_path(header[0]):
            if header[1]:
                raise ValueError(
                    "mcp_servers.reviewer нельзя безопасно обновить: "
                    "массив TOML-таблиц не поддерживается"
                )
            if insert_at is None:
                insert_at = len(output)
            removed = True
            index += 1
            while index < len(lines) and index not in headers:
                index += 1
            continue
        output.append(lines[index])
        index += 1
    existing = parsed.get("mcp_servers") if isinstance(parsed, dict) else None
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("mcp_servers должен быть TOML-таблицей")
    has_inline = isinstance(existing, dict) and "reviewer" in existing and not removed
    if has_inline:
        raise ValueError("inline mcp_servers.reviewer нельзя безопасно обновить")
    block = rendered.lstrip("\n")
    if insert_at is None:
        content = "".join(output)
        separator = "" if not content or content.endswith(("\n", "\r")) else "\n"
        content += separator + block
    else:
        output.insert(insert_at, block)
        content = "".join(output)
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"mcp_servers нельзя безопасно обновить: {exc}"
        ) from exc
    return content, removed


@dataclass
class InstallPlan:
    client: Client
    path: Path
    content: str          # что будет записано в файл целиком
    command: str
    args: list[str]
    created: bool         # файла не было — создаём
    already: bool         # запись reviewer уже была (перезапишем)


@dataclass
class AllowlistPlan:
    path: Path
    content: str          # что будет записано в файл целиком
    created: bool         # файла не было — создаём
    already: bool         # правило уже было в permissions.allow


def build_plan(
    client: Client,
    *,
    system: str | None = None,
    version: str = "latest",
    path_override: str | None = None,
) -> InstallPlan:
    """Подготовить план установки (без записи на диск)."""
    system = system or platform.system()
    path = Path(path_override).expanduser() if path_override else client.path_fn(system)
    command, args = launch_command(version)
    existed = path.exists()
    raw = path.read_bytes().decode("utf-8") if existed else ""

    if client.dialect == "codex":
        content, already = _replace_codex_reviewer_tables(raw, _render_codex(command, args))
        return InstallPlan(client, path, content, command, args, not existed, already)

    cfg = json.loads(raw) if raw.strip() else {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: ожидался JSON-объект на верхнем уровне")
    top = _TOP_KEY[client.dialect]
    section = cfg.get(top)
    if not isinstance(section, dict):
        section = {}
    already = SERVER_NAME in section
    section[SERVER_NAME] = _entry(client.dialect, command, args)
    cfg[top] = section
    content = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    return InstallPlan(client, path, content, command, args, not existed, already)


def build_allowlist_plan(
    path: Path | None = None, rule: str = REVIEWER_PERMISSION_RULE
) -> AllowlistPlan:
    """План мёрджа allow-правила reviewer в permissions.allow Claude Code settings.

    Сохраняет чужие ключи и существующие правила; идемпотентен (already=True,
    если правило уже присутствует). Запись на диск — apply_allowlist_plan.
    """
    path = path or claude_settings_path()
    existed = path.exists()
    raw = path.read_text(encoding="utf-8") if existed else ""
    cfg = json.loads(raw) if raw.strip() else {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: ожидался JSON-объект на верхнем уровне")
    perms = cfg.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
    already = rule in allow
    if not already:
        allow = [*allow, rule]
    perms["allow"] = allow
    cfg["permissions"] = perms
    content = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    return AllowlistPlan(path, content, not existed, already)


def _write_with_backup(path: Path, content: str) -> Path | None:
    """Записать content в path; при изменении существующего файла сделать .bak.

    Возвращает путь к .bak (если был бэкап) или None. Ничего не пишет и не
    бэкапит, если содержимое не изменилось.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    if path.exists():
        existing = path.read_bytes()
        if existing == encoded:
            return None
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_bytes(existing)
        path.write_bytes(encoded)
        return backup
    path.write_bytes(encoded)
    return None


def apply_plan(plan: InstallPlan) -> Path | None:
    """Записать MCP-план на диск. Возвращает путь к .bak (если был бэкап)."""
    return _write_with_backup(plan.path, plan.content)


def apply_allowlist_plan(plan: AllowlistPlan) -> Path | None:
    """Записать allowlist-план на диск. Возвращает путь к .bak (если был бэкап)."""
    return _write_with_backup(plan.path, plan.content)


def detect_installed(system: str | None = None) -> list[Client]:
    """Клиенты с конфигами, которые, похоже, установлены.

    Эвристика: существует родительский каталог конфига. Project- и native-
    клиенты исключены: Claude Code определяется по доступности своего публичного
    CLI в entrypoints.cli, а не по каталогу текущего проекта.
    """
    system = system or platform.system()
    found: list[Client] = []
    for client in CLIENTS.values():
        if client.scope != "user":
            continue
        if client.path_fn(system).parent.exists():
            found.append(client)
    return found


# --------------------------------------------------------------------------- #
# скилы
# --------------------------------------------------------------------------- #
def fetch_skills_archive(url: str = SKILLS_TARBALL) -> tuple[bytes, str | None]:
    """Скачать тарбол репозитория + вернуть его ETag (для стампа). httpx уже в зависимостях."""
    import httpx

    resp = httpx.get(url, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    return resp.content, resp.headers.get("etag")


def fetch_skills_bytes(url: str = SKILLS_TARBALL) -> bytes:
    """Только тарбол (обратная совместимость со старыми вызовами)."""
    return fetch_skills_archive(url)[0]


def fetch_skills_etag(url: str = SKILLS_TARBALL, *, timeout: float = 5.0) -> str | None:
    """ETag тарбола через HEAD. Fail-soft: при любой ошибке/офлайне — None."""
    import httpx

    try:
        resp = httpx.head(url, follow_redirects=True, timeout=timeout)
        resp.raise_for_status()
        return resp.headers.get("etag")
    except Exception:  # noqa: BLE001 — детект устарелости не должен падать
        return None


def extract_skills(tar_bytes: bytes, dest: Path) -> list[str]:
    """Распаковать plugin/skills/* из тарбола в каталог dest. Вернуть имена скилов.

    Защита от path traversal: целевые пути обязаны лежать внутри dest.
    """
    import io
    import tarfile

    dest = dest.resolve()
    names: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            parts = m.name.split("/")
            if "plugin" not in parts:
                continue
            i = parts.index("plugin")
            if parts[i:i + 2] != ["plugin", "skills"] or len(parts) <= i + 2:
                continue
            rel = "/".join(parts[i + 2:])          # <skill>/<...>
            target = (dest / rel).resolve()
            if not str(target).startswith(str(dest) + os.sep):
                continue                           # выходит за dest — пропускаем
            fobj = tf.extractfile(m)
            if fobj is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(fobj.read())
            names.add(parts[i + 2])
    return sorted(names)


def install_skills(
    client: Client,
    *,
    system: str | None = None,
    tar_bytes: bytes | None = None,
    source_etag: str | None = None,
) -> tuple[Path, list[str]]:
    """Установить скилы в каталог клиента + записать стамп. Возвращает (каталог, имена).

    tar_bytes можно передать заранее скачанным (чтобы не качать на каждый клиент);
    в этом случае передайте и source_etag (иначе он будет None в стампе).
    """
    system = system or platform.system()
    if client.skills_fn is None:
        raise ValueError(f"{client.label}: файловые скилы не поддерживаются")
    dest = client.skills_fn(system)
    if tar_bytes is None:
        data, fetched_etag = fetch_skills_archive()
        if source_etag is None:
            source_etag = fetched_etag
    else:
        data = tar_bytes
    names = extract_skills(data, dest)
    stamp_skills_dir(dest, source_etag=source_etag)
    return dest, names


# --------------------------------------------------------------------------- #
# стамп установки скилов (для детекта устарелости)
# --------------------------------------------------------------------------- #
STAMP_NAME = ".reviewer-skills.json"


def current_pkg_version() -> str:
    """Версия установленного пакета rag-reviewer (или 'unknown')."""
    import importlib.metadata as md

    try:
        return md.version(PACKAGE)
    except md.PackageNotFoundError:
        return "unknown"


def _skill_file_hashes(skills_dir: Path) -> dict[str, str]:
    """sha256 каждого скила (по всем его файлам). Ключ — имя подкаталога-скила.

    Детерминизм: файлы скила сортируются по относительному пути, в дайджест идёт
    rel-path + NUL + содержимое. Не-каталоги верхнего уровня (включая сам
    стамп-файл) пропускаются.
    """
    import hashlib

    result: dict[str, str] = {}
    if not skills_dir.is_dir():
        return result
    for sub in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        h = hashlib.sha256()
        for f in sorted(sub.rglob("*")):
            if f.is_file():
                h.update(f.relative_to(sub).as_posix().encode("utf-8"))
                h.update(b"\0")
                h.update(f.read_bytes())
        result[sub.name] = "sha256:" + h.hexdigest()
    return result


def write_skills_stamp(
    skills_dir: Path, *, source_url: str, source_etag: str | None,
    pkg_version: str, hashes: dict[str, str],
) -> Path:
    """Записать стамп установки скилов в <skills_dir>/.reviewer-skills.json."""
    from datetime import datetime, timezone

    stamp = {
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source_url": source_url,
        "source_etag": source_etag,
        "pkg_version": pkg_version,
        "skills": hashes,
    }
    path = skills_dir / STAMP_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_skills_stamp(skills_dir: Path) -> dict | None:
    """Прочитать стамп; None, если файла нет или он битый."""
    path = skills_dir / STAMP_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def stamp_skills_dir(skills_dir: Path, *, source_etag: str | None) -> Path:
    """Записать стамп для уже распакованного каталога скилов."""
    return write_skills_stamp(
        skills_dir,
        source_url=SKILLS_TARBALL,
        source_etag=source_etag,
        pkg_version=current_pkg_version(),
        hashes=_skill_file_hashes(skills_dir),
    )


# --------------------------------------------------------------------------- #
# детект устарелости скилов
# --------------------------------------------------------------------------- #
@dataclass
class StalenessReport:
    client_key: str
    client_label: str
    skills_dir: Path
    stale: bool
    reason: str            # человекочитаемая причина (пусто, если свежо)
    command: str           # рекомендуемая команда исправления


# сигнал «получи ETag сам»; отличаем от явного None (= офлайн/недоступен)
_FETCH_ETAG: object = object()


def skills_staleness(
    client: Client,
    *,
    system: str | None = None,
    timeout: float = 5.0,
    upstream_etag: str | None | object = _FETCH_ETAG,
) -> StalenessReport | None:
    """Оценить, устарели ли установленные скилы клиента.

    None — у клиента нет файловых скилов или каталог не существует (нечего
    проверять). Иначе StalenessReport. Сетевой ETag — best-effort: при офлайне
    используется фолбэк по версии пакета. upstream_etag можно передать заранее
    полученным (staleness_warnings берёт его один раз на весь обход), чтобы не
    слать HEAD на каждого клиента; _FETCH_ETAG — получить самостоятельно.
    """
    system = system or platform.system()
    if client.skills_fn is None:
        return None
    skills_dir = client.skills_fn(system)
    if not skills_dir.exists():
        return None
    cmd = f"reviewer install-skills {client.key}"

    def report(stale: bool, reason: str) -> StalenessReport:
        return StalenessReport(client.key, client.label, skills_dir, stale, reason, cmd)

    stamp = read_skills_stamp(skills_dir)
    if stamp is None:
        return report(True, "нет стампа установки (старый установщик)")
    if _skill_file_hashes(skills_dir) != (stamp.get("skills") or {}):
        return report(True, "содержимое скилов разошлось со стампом (дрейф/частичная установка)")
    etag = fetch_skills_etag(timeout=timeout) if upstream_etag is _FETCH_ETAG else upstream_etag
    if etag is not None:
        if stamp.get("source_etag") and etag != stamp["source_etag"]:
            return report(True, "upstream main обновился с момента установки")
        return report(False, "")
    # офлайн-фолбэк: сравнить версию пакета на момент установки и текущую
    cur = current_pkg_version()
    stamp_ver = stamp.get("pkg_version")
    if cur != "unknown" and stamp_ver and stamp_ver != "unknown" and cur != stamp_ver:
        return report(
            True,
            f"сервер обновился ({stamp_ver}→{cur}), upstream недоступен офлайн")
    return report(False, "")


def staleness_warnings(system: str | None = None, *, timeout: float = 5.0) -> list[str]:
    """Строки-предупреждения по установленным клиентам с файловыми скилами (fail-soft).

    Upstream-ETag берётся ОДИН раз на весь обход (а не HEAD на каждого клиента);
    сеть не дёргается вовсе, если ни у кого нет каталога скилов.
    """
    system = system or platform.system()
    candidates = [
        c for c in CLIENTS.values()
        if c.skills_fn is not None and c.scope != "project" and c.skills_fn(system).exists()
    ]
    if not candidates:
        return []
    upstream_etag = fetch_skills_etag(timeout=timeout)  # один HEAD на весь обход
    lines: list[str] = []
    for client in candidates:
        try:
            rep = skills_staleness(
                client, system=system, timeout=timeout, upstream_etag=upstream_etag)
        except Exception:  # noqa: BLE001 — детект не должен ломать вызывающего
            continue
        if rep and rep.stale:
            lines.append(f"⚠ скилы {rep.client_label} устарели ({rep.reason}) → {rep.command}")
    return lines
