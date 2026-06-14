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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PACKAGE = "rag-reviewer"
SERVER_NAME = "reviewer"

# Скилы лежат в репозитории (plugin/skills) и в wheel не пакуются — тянем их
# тарболом с GitHub (кроссплатформенно, без клона).
SKILLS_TARBALL = "https://github.com/mimfort/rag_for_git/archive/refs/heads/main.tar.gz"
SKILL_NAMES = (
    "review-pr", "solve-task", "sync-codebase",
    "sync-tasks", "performance-review", "maintainability-review",
)

# Шаблон .env (встроен, чтобы `reviewer init` работал из published-wheel без
# упаковки файла). Все остальные переменные имеют дефолты в settings.py.
ENV_TEMPLATE = """\
# rag_for_git — конфигурация. Обязателен только VOYAGE_API_KEY; GITHUB_TOKEN
# нужен для ревью PR. Остальное имеет дефолты в reviewer/config/settings.py.

# --- Voyage (эмбеддинги + реранкер) — обязательно ---
VOYAGE_API_KEY=

# --- GitHub (PAT: Pull requests read/write, Contents read) ---
GITHUB_TOKEN=

# --- Postgres (ParadeDB :5433) / Neo4j (:7687) — дефолты docker-compose ---
PG_DSN=postgresql://reviewer:reviewer@localhost:5433/reviewer
NEO4J_URI=neo4j://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=reviewerpass

# --- Граф кода: auto|scip|treesitter ---
GRAPH_BACKEND=auto

# --- Мульти-репо / ветки (опционально) ---
DEFAULT_REPO=
REVIEW_BRANCHES=main,master
"""


def default_env_path() -> Path:
    """Каноническое место .env: $XDG_CONFIG_HOME/rag-reviewer/.env."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(xdg) / "rag-reviewer" / ".env"


# --------------------------------------------------------------------------- #
# команда запуска
# --------------------------------------------------------------------------- #
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
        return uvx, tail
    uv = shutil.which("uv")
    if uv:
        return uv, ["tool", "run", *tail]
    return "uvx", tail  # fallback: надеемся на PATH


# --------------------------------------------------------------------------- #
# реестр клиентов
# --------------------------------------------------------------------------- #
def _home() -> Path:
    return Path.home()


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
    dialect: str  # mcpServers | vscode | mimo | opencode | codex
    path_fn: Callable[[str], Path]
    scope: str = "user"  # user | project
    note: str = ""
    # каталог глобальных скилов клиента (None — файловые скилы не поддерживаются)
    skills_fn: Callable[[str], Path] | None = None


CLIENTS: dict[str, Client] = {
    c.key: c
    for c in [
        Client("cursor", "Cursor", "mcpServers", lambda s: _home() / ".cursor" / "mcp.json"),
        Client("claude-desktop", "Claude Desktop", "mcpServers", _claude_desktop_path),
        Client("claude-code", "Claude Code", "mcpServers",
               lambda s: Path(".mcp.json"), scope="project",
               note="проектный .mcp.json в текущем каталоге; либо ставьте плагин "
                    "через /plugin marketplace add mimfort/rag_for_git"),
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
               lambda s: _home() / ".codex" / "config.toml"),
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


def _render_codex(command: str, args: list[str]) -> str:
    args_toml = ", ".join(json.dumps(a) for a in args)
    return (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f"command = {json.dumps(command)}\n"
        f"args = [{args_toml}]\n"
    )


@dataclass
class InstallPlan:
    client: Client
    path: Path
    content: str          # что будет записано в файл целиком
    command: str
    args: list[str]
    created: bool         # файла не было — создаём
    already: bool         # запись reviewer уже была (перезапишем)


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
    raw = path.read_text(encoding="utf-8") if existed else ""

    if client.dialect == "codex":
        already = f"[mcp_servers.{SERVER_NAME}]" in raw
        content = raw if already else (raw.rstrip("\n") + ("\n" if raw.strip() else "")
                                       + _render_codex(command, args))
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


def apply_plan(plan: InstallPlan) -> Path | None:
    """Записать план на диск. Возвращает путь к .bak (если был бэкап).

    Если содержимое не меняется (запись уже актуальна / TOML с существующей
    секцией), ничего не пишем и не бэкапим.
    """
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    if plan.path.exists():
        existing = plan.path.read_text(encoding="utf-8")
        if existing == plan.content:
            return None
        backup = plan.path.with_suffix(plan.path.suffix + ".bak")
        backup.write_text(existing, encoding="utf-8")
        plan.path.write_text(plan.content, encoding="utf-8")
        return backup
    plan.path.write_text(plan.content, encoding="utf-8")
    return None


def detect_installed(system: str | None = None) -> list[Client]:
    """Клиенты, которые, похоже, установлены (есть каталог конфига/приложения).

    Эвристика: существует родительский каталог конфига. claude-code (проектный)
    из автодетекта исключён — он всегда «доступен», но писать .mcp.json в CWD
    по умолчанию не нужно.
    """
    system = system or platform.system()
    found: list[Client] = []
    for client in CLIENTS.values():
        if client.scope == "project":
            continue
        if client.path_fn(system).parent.exists():
            found.append(client)
    return found


# --------------------------------------------------------------------------- #
# скилы
# --------------------------------------------------------------------------- #
def fetch_skills_bytes(url: str = SKILLS_TARBALL) -> bytes:
    """Скачать тарбол репозитория (httpx уже в зависимостях; кроссплатформенно)."""
    import httpx

    resp = httpx.get(url, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    return resp.content


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
) -> tuple[Path, list[str]]:
    """Установить скилы в каталог клиента. Возвращает (каталог, имена скилов).

    tar_bytes можно передать заранее скачанным (чтобы не качать на каждый клиент).
    """
    system = system or platform.system()
    if client.skills_fn is None:
        raise ValueError(f"{client.label}: файловые скилы не поддерживаются")
    dest = client.skills_fn(system)
    data = tar_bytes if tar_bytes is not None else fetch_skills_bytes()
    names = extract_skills(data, dest)
    return dest, names
