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
from dataclasses import dataclass, field as _field
from pathlib import Path
from typing import Callable

PACKAGE = "rag-reviewer"
SERVER_NAME = "reviewer"

# anchored-glob правило, разрешающее ВЕСЬ сервер reviewer в Claude Code
# (permissions.allow). Wildcard покрывает новые тулы автоматически — список
# тулов синхронизировать не нужно.
REVIEWER_PERMISSION_RULE = "mcp__reviewer__*"

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


@dataclass
class EnvField:
    key: str
    prompt_text: str
    default: str = ""
    secret: bool = False
    required: bool = False


@dataclass
class EnvGroup:
    title: str
    fields: list[EnvField] = _field(default_factory=list)
    optional: bool = False


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
            EnvField(
                key="GITHUB_TOKEN",
                prompt_text="GITHUB_TOKEN (PAT: Pull requests read/write, Contents read)",
                secret=True,
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
    EnvGroup(
        title="Мульти-репо / ветки",
        optional=True,
        fields=[
            EnvField(
                key="DEFAULT_REPO",
                prompt_text="DEFAULT_REPO (owner/name или пусто)",
                default="",
            ),
            EnvField(
                key="REVIEW_BRANCHES",
                prompt_text="REVIEW_BRANCHES (CSV, первая — первичная)",
                default="main,master",
            ),
        ],
    ),
    EnvGroup(
        title="Доска задач",
        optional=True,
        fields=[
            EnvField(
                key="TASK_BOARD_TYPE",
                prompt_text="TASK_BOARD_TYPE (yougile | jira | ...)",
                default="",
            ),
            EnvField(
                key="TASK_BOARD_MCP",
                prompt_text="TASK_BOARD_MCP (имя MCP-сервера доски)",
                default="",
            ),
            EnvField(
                key="TASK_BOARD_KEY_PATTERN",
                prompt_text=r"TASK_BOARD_KEY_PATTERN (напр. [A-Z]+-\d+)",
                default="",
            ),
            EnvField(
                key="TASK_BOARD_URL_TEMPLATE",
                prompt_text="TASK_BOARD_URL_TEMPLATE (напр. https://.../{code})",
                default="",
            ),
        ],
    ),
]


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
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return None
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(existing, encoding="utf-8")
        path.write_text(content, encoding="utf-8")
        return backup
    path.write_text(content, encoding="utf-8")
    return None


def apply_plan(plan: InstallPlan) -> Path | None:
    """Записать MCP-план на диск. Возвращает путь к .bak (если был бэкап)."""
    return _write_with_backup(plan.path, plan.content)


def apply_allowlist_plan(plan: AllowlistPlan) -> Path | None:
    """Записать allowlist-план на диск. Возвращает путь к .bak (если был бэкап)."""
    return _write_with_backup(plan.path, plan.content)


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
