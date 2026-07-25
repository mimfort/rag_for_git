# PRI-218 Global Interactive Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить устанавливаемую из PyPI compact command palette для no-args TTY-вызова `reviewer`, не меняя прямые Click/MCP-контракты.

**Architecture:** Новый `reviewer.entrypoints.launcher` выполняет fail-closed routing и лениво открывает `prompt_toolkit` UI только в настоящем интерактивном терминале. Каталог и argv строятся из существующего Click command tree с отдельным presentation metadata, а после явного подтверждения команда исполняется тем же Click `cli` in-process.

**Tech Stack:** Python 3.11–3.13, Click 8.1+, prompt_toolkit 3.x, pytest, uv/uvx, setuptools, GitHub Actions.

## Global Constraints

- `prompt_toolkit>=3.0,<4` — core dependency, не optional extra.
- `reviewer` из `uv tool install rag-reviewer` и `uvx --from rag-reviewer@latest reviewer` работает из любого каталога без checkout репозитория.
- Любой непустой argv, non-TTY, `TERM=dumb` или CI немедленно делегируется существующему Click CLI без launcher output и без импорта prompt_toolkit.
- `reviewer-mcp = "reviewer.entrypoints.mcp_server:main"` и его import path не меняются.
- Все видимые Click-команды автоматически входят в каталог; Click остаётся authority для параметров, defaults и финальной validation.
- Любая команда требует details → masked preview → отдельный confirm; shell не используется.
- `Esc` из palette завершает с кодом 0, `Ctrl+C` — с кодом 130, ошибка TUI — с кодом 1 и одной строкой в stderr.
- До command confirm нет writes/subprocess; сеть допустима только после отдельного подтверждения «Проверить обновления».
- Прямой `reviewer update` сохраняет текущий output и mutation semantics.
- Platform distribution gate: Ubuntu, macOS и Windows, Python 3.11, wheel + изолированные `uv tool install` и `uvx --from`.
- Новые комментарии, docstrings и user-facing тексты пишутся по-русски.

---

### Task 1: Fail-closed dispatcher и lazy boundary

**Files:**
- Create: `reviewer/launcher/__init__.py`
- Create: `reviewer/launcher/models.py`
- Create: `reviewer/entrypoints/launcher.py`
- Test: `tests/entrypoints/test_launcher.py`

**Interfaces:**
- Consumes: существующий `reviewer.entrypoints.cli:cli`.
- Produces: `LauncherResult(argv: tuple[str, ...] | None, exit_code: int)`;
  `should_use_tui(argv: Sequence[str], *, stdin: object, stdout: object, environ: Mapping[str, str]) -> bool`;
  `main(argv: Sequence[str] | None = None) -> None`.

- [ ] **Step 1: Написать failing routing tests**

```python
@pytest.mark.parametrize(
    ("argv", "stdin_tty", "stdout_tty", "env", "expected"),
    [
        (["--help"], True, True, {}, False),
        ([], False, True, {}, False),
        ([], True, False, {}, False),
        ([], True, True, {"TERM": "dumb"}, False),
        ([], True, True, {"CI": "true"}, False),
        ([], True, True, {"GITHUB_ACTIONS": "true"}, False),
        ([], True, True, {"CI": "false"}, True),
        ([], True, True, {}, True),
    ],
)
def test_should_use_tui_matrix(argv, stdin_tty, stdout_tty, env, expected):
    assert should_use_tui(
        argv,
        stdin=_Stream(stdin_tty),
        stdout=_Stream(stdout_tty),
        environ=env,
    ) is expected
```

Добавить отдельные cases, где `isatty()` бросает `OSError`: оба должны вернуть `False`.

- [ ] **Step 2: Запустить RED для routing**

Run:

```bash
.venv/bin/pytest tests/entrypoints/test_launcher.py -q
```

Expected: collection/import FAIL, потому что `reviewer.entrypoints.launcher` ещё не существует.

- [ ] **Step 3: Реализовать минимальный gate**

```python
_FALSEY_ENV = {"", "0", "false", "no", "off"}
_CI_MARKERS = (
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "TF_BUILD",
    "BUILDKITE",
    "CIRCLECI",
    "JENKINS_URL",
    "TEAMCITY_VERSION",
)


def _safe_isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _in_ci(environ: Mapping[str, str]) -> bool:
    ci = environ.get("CI")
    if ci is not None and ci.strip().casefold() not in _FALSEY_ENV:
        return True
    return any(environ.get(name) for name in _CI_MARKERS)


def should_use_tui(
    argv: Sequence[str],
    *,
    stdin: object,
    stdout: object,
    environ: Mapping[str, str],
) -> bool:
    return (
        not argv
        and _safe_isatty(stdin)
        and _safe_isatty(stdout)
        and environ.get("TERM", "").strip().casefold() != "dumb"
        and not _in_ci(environ)
    )
```

`LauncherResult` должен быть frozen dataclass; поле с реальным argv пометить `repr=False`.

- [ ] **Step 4: Проверить GREEN routing**

Run:

```bash
.venv/bin/pytest tests/entrypoints/test_launcher.py -q
```

Expected: routing matrix PASS.

- [ ] **Step 5: Добавить failing dispatch/lazy tests**

```python
def test_direct_route_forwards_original_argv_without_loading_tui(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_run_cli", lambda argv: calls.append(tuple(argv)))
    monkeypatch.setattr(
        launcher,
        "_run_tui",
        lambda: pytest.fail("TUI не должен загружаться"),
    )

    launcher.main(["status", "--json"])

    assert calls == [("status", "--json")]


def test_interactive_confirm_runs_existing_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "should_use_tui", lambda *a, **k: True)
    monkeypatch.setattr(
        launcher,
        "_run_tui",
        lambda: LauncherResult(("check", "--board-project", "yougile=PRI"), 0),
    )
    monkeypatch.setattr(launcher, "_run_cli", lambda argv: calls.append(tuple(argv)))

    launcher.main([])

    assert calls == [("check", "--board-project", "yougile=PRI")]
```

Также проверить cancel `SystemExit(0)`, `KeyboardInterrupt → SystemExit(130)` и TUI exception:
ровно одна русская строка в stderr с `reviewer --help`, `SystemExit(1)`, Click не вызван.

- [ ] **Step 6: Запустить RED для dispatch**

Run:

```bash
.venv/bin/pytest tests/entrypoints/test_launcher.py -q
```

Expected: FAIL, потому что `main`, `_run_cli` и `_run_tui` ещё не реализованы.

- [ ] **Step 7: Реализовать dispatch без prompt_toolkit import**

```python
def _run_cli(argv: Sequence[str]) -> None:
    from reviewer.entrypoints.cli import cli

    cli.main(args=list(argv), prog_name="reviewer", standalone_mode=True)


def _run_tui() -> LauncherResult:
    from reviewer.launcher.app import run_launcher

    return run_launcher()


def main(argv: Sequence[str] | None = None) -> None:
    resolved = tuple(sys.argv[1:] if argv is None else argv)
    if not should_use_tui(
        resolved,
        stdin=sys.stdin,
        stdout=sys.stdout,
        environ=os.environ,
    ):
        _run_cli(resolved)
        return
    try:
        result = _run_tui()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        print(
            "reviewer: интерактивный launcher недоступен; используйте reviewer --help",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if result.argv is None:
        raise SystemExit(result.exit_code)
    _run_cli(result.argv)
```

Импорт отсутствующего пока `reviewer.launcher.app` допустим: public entry point в
`pyproject.toml` меняется только в Task 5.

- [ ] **Step 8: Проверить Task 1**

Run:

```bash
.venv/bin/pytest tests/entrypoints/test_launcher.py -q
.venv/bin/ruff check reviewer/entrypoints/launcher.py reviewer/launcher tests/entrypoints/test_launcher.py
```

Expected: PASS, lint clean.

- [ ] **Step 9: Commit**

```bash
git add reviewer/entrypoints/launcher.py reviewer/launcher tests/entrypoints/test_launcher.py
git commit -m "feat(cli): добавить безопасный dispatcher launcher"
```

---

### Task 2: Click catalog, presentation metadata и безопасный argv

**Files:**
- Modify: `reviewer/launcher/models.py`
- Create: `reviewer/launcher/metadata.py`
- Create: `reviewer/launcher/catalog.py`
- Create: `reviewer/launcher/command.py`
- Create: `tests/launcher/__init__.py`
- Create: `tests/launcher/test_catalog.py`
- Create: `tests/launcher/test_command.py`

**Interfaces:**
- Consumes: `reviewer.entrypoints.cli:cli`, `click.Group`, `click.Parameter`.
- Produces: `build_catalog(root: click.Group) -> tuple[CommandSpec, ...]`;
  `prepare_command(spec: CommandSpec, values: Mapping[str, object], changed: AbstractSet[str], *, platform_name: str | None = None) -> PreparedCommand`.

- [ ] **Step 1: Написать failing catalog tests**

```python
VISIBLE_COMMANDS = {
    "check",
    "gc",
    "index",
    "init",
    "install",
    "install-skills",
    "migrate-branches",
    "search",
    "serve",
    "status",
    "update",
}


def test_catalog_contains_every_visible_click_command():
    catalog = build_catalog(cli)
    assert {" ".join(item.path) for item in catalog} == VISIBLE_COMMANDS


def test_status_schema_comes_from_click():
    status = next(item for item in build_catalog(cli) if item.path == ("status",))
    by_name = {param.name: param for param in status.params}
    assert by_name["path"].default == "."
    assert by_name["repo_tag"].option_strings == ("--repo",)
    assert by_name["json_output"].is_flag is True
```

Добавить hidden/deprecated fixture group, callable default sentinel и тест, что callable не
вызывается при `build_catalog`.

- [ ] **Step 2: Запустить RED catalog**

Run:

```bash
.venv/bin/pytest tests/launcher/test_catalog.py -q
```

Expected: import FAIL для `reviewer.launcher.catalog`.

- [ ] **Step 3: Добавить immutable models и catalog traversal**

```python
class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


class ParamSection(StrEnum):
    BASIC = "basic"
    ADVANCED = "advanced"


@dataclass(frozen=True)
class CommandPresentation:
    summary: str
    details: str
    effects: tuple[Effect, ...]
    scenarios: tuple[str, ...]
    keywords: tuple[str, ...]
    special_action: str | None = None


@dataclass(frozen=True)
class ParameterPresentation:
    section: ParamSection = ParamSection.BASIC
    sensitive: bool = False
    description: str | None = None


@dataclass(frozen=True)
class ParameterSpec:
    source: click.Parameter = field(repr=False, compare=False)
    name: str
    kind: str
    option_strings: tuple[str, ...]
    secondary_strings: tuple[str, ...]
    required: bool
    nargs: int
    multiple: bool
    count: bool
    is_flag: bool
    default: object = field(repr=False)
    choices: tuple[str, ...]
    section: ParamSection
    sensitive: bool


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    command: click.Command = field(repr=False, compare=False)
    summary: str
    details: str
    effects: tuple[Effect, ...]
    scenarios: tuple[str, ...]
    keywords: tuple[str, ...]
    params: tuple[ParameterSpec, ...]
    special_action: str | None = None
```

`ParameterSpec` должен хранить `source: click.Parameter` с `repr=False`, name, kind,
option_strings, secondary_strings, required, nargs, multiple, count, is_flag, static default,
choices, section и sensitive.

Traversal использует новый `click.Context(command, info_name=command.name or "reviewer")` на каждом уровне,
`list_commands`/`get_command`, пропускает `hidden=True` и рекурсивно разворачивает nested Group.
Static default читается через `parameter.get_default(ctx, call=False)`.

- [ ] **Step 4: Добавить полный metadata registry**

```python
COMMAND_PRESENTATION = {
    ("check",): CommandPresentation(
        summary="Проверить окружение",
        details="Проверяет ключи, хранилища, VCS и настроенные доски без изменения данных.",
        effects=(Effect.READ, Effect.NETWORK),
        scenarios=("После установки", "После изменения .env"),
        keywords=("health", "диагностика", "доступы"),
    ),
    ("gc",): CommandPresentation(
        summary="Очистить осиротевшие overlay",
        details="Удаляет только overlay без живой review session и просроченные сессии.",
        effects=(Effect.DESTRUCTIVE,),
        scenarios=("После прерванных ревью",),
        keywords=("cleanup", "sessions", "overlay"),
    ),
    ("index",): CommandPresentation(
        summary="Построить индекс кодовой базы",
        details="Индексирует выбранную git-ветку в векторное хранилище и граф кода.",
        effects=(Effect.WRITE, Effect.NETWORK),
        scenarios=("Первичная настройка", "Обновление base-индекса"),
        keywords=("rag", "graph", "branch"),
    ),
    ("init",): CommandPresentation(
        summary="Настроить reviewer",
        details="Создаёт или обновляет user-scope .env через существующий setup wizard.",
        effects=(Effect.WRITE,),
        scenarios=("Первичная настройка", "Смена credentials"),
        keywords=("config", "env", "wizard"),
    ),
    ("install",): CommandPresentation(
        summary="Подключить reviewer к AI-клиенту",
        details="Устанавливает MCP-конфигурацию и доступные plugin skills выбранного клиента.",
        effects=(Effect.WRITE, Effect.NETWORK),
        scenarios=("Подключение Codex/Claude/IDE",),
        keywords=("mcp", "plugin", "client"),
    ),
    ("install-skills",): CommandPresentation(
        summary="Установить только skills",
        details="Обновляет skills или plugin выбранного клиента, не меняя reviewer engine.",
        effects=(Effect.WRITE, Effect.NETWORK),
        scenarios=("Обновление workflow skills",),
        keywords=("skills", "plugin", "codex"),
    ),
    ("migrate-branches",): CommandPresentation(
        summary="Мигрировать legacy base-index",
        details="Переименовывает legacy ref base в primary branch ref после обновления.",
        effects=(Effect.WRITE,),
        scenarios=("Однократная миграция multi-branch"),
        keywords=("migration", "base", "branch"),
    ),
    ("search",): CommandPresentation(
        summary="Найти код в base-индексе",
        details="Выполняет гибридный semantic/lexical поиск по выбранной ветке.",
        effects=(Effect.READ, Effect.NETWORK),
        scenarios=("Диагностика retrieval", "Поиск символов"),
        keywords=("query", "rag", "code"),
    ),
    ("serve",): CommandPresentation(
        summary="Запустить web-админку",
        details="Запускает локальный FastAPI-сервер наблюдаемости на заданном host/port.",
        effects=(Effect.NETWORK,),
        scenarios=("Просмотр истории ревью",),
        keywords=("web", "history", "dashboard"),
    ),
    ("status",): CommandPresentation(
        summary="Проверить свежесть индекса",
        details="Показывает SHA, drift, chunks, graph nodes и overlay без Voyage-затрат.",
        effects=(Effect.READ,),
        scenarios=("Перед solve-task", "Диагностика индекса"),
        keywords=("drift", "json", "health"),
    ),
    ("update",): CommandPresentation(
        summary="Проверить обновления",
        details="По явному действию сравнивает установленную и последнюю PyPI-версию.",
        effects=(Effect.READ, Effect.NETWORK),
        scenarios=("Обновление глобальной uv tool-установки",),
        keywords=("pypi", "version", "upgrade"),
        special_action="check_update",
    ),
}
```

`COMMAND_PRESENTATION` перечисляет все 11 ключей из `VISIBLE_COMMANDS`.
Parameter metadata должно маркировать
advanced operational flags и future-sensitive fields; если текущая команда не принимает secret
option, не выдумывать новый CLI-параметр.

- [ ] **Step 5: Добавить metadata guard и проверить GREEN catalog**

Добавить assertions:

```python
def test_current_commands_have_rich_metadata_without_orphans():
    paths = {item.path for item in build_catalog(cli)}
    assert set(COMMAND_PRESENTATION) == paths
```

Run:

```bash
.venv/bin/pytest tests/launcher/test_catalog.py -q
```

Expected: PASS.

- [ ] **Step 6: Написать failing argv/masking tests**

```python
def _command(name: str) -> CommandSpec:
    return next(item for item in build_catalog(cli) if item.path == (name,))


def _secret_command() -> CommandSpec:
    source = click.Option(["--token"])
    parameter = ParameterSpec(
        source=source,
        name="token",
        kind="option",
        option_strings=("--token",),
        secondary_strings=(),
        required=False,
        nargs=1,
        multiple=False,
        count=False,
        is_flag=False,
        default=None,
        choices=(),
        section=ParamSection.BASIC,
        sensitive=True,
    )
    return CommandSpec(
        path=("deploy",),
        command=click.Command("deploy", params=[source]),
        summary="Deploy",
        details="Test command",
        effects=(Effect.WRITE,),
        scenarios=(),
        keywords=(),
        params=(parameter,),
    )


def test_prepare_status_omits_unchanged_defaults_and_emits_changed_flags():
    status = _command("status")
    prepared = prepare_command(
        status,
        values={"path": ".", "repo_tag": "a/x", "json_output": True},
        changed={"repo_tag", "json_output"},
        platform_name="Linux",
    )
    assert prepared.argv == ("status", "--repo", "a/x", "--json")
    assert prepared.preview == "reviewer status --repo a/x --json"


def test_sensitive_value_is_real_in_argv_but_masked_everywhere_else():
    spec = _secret_command()
    prepared = prepare_command(
        spec,
        values={"token": "secret-123"},
        changed={"token"},
        platform_name="Linux",
    )
    assert prepared.argv == ("deploy", "--token", "secret-123")
    assert "secret-123" not in prepared.preview
    assert "secret-123" not in repr(prepared)
    assert "••••••" in prepared.preview
```

Параметризованный test создаёт test-only Click commands для required positional, optional
positional omission, secondary boolean flag, multiple option, count и fixed nargs. Отдельный
Windows case ожидает `subprocess.list2cmdline(("reviewer", *prepared.argv))` после замены
sensitive tokens.

- [ ] **Step 7: Запустить RED argv**

Run:

```bash
.venv/bin/pytest tests/launcher/test_command.py -q
```

Expected: import FAIL для `reviewer.launcher.command`.

- [ ] **Step 8: Реализовать token builder и display-only preview**

```python
@dataclass(frozen=True)
class PreparedCommand:
    argv: tuple[str, ...] = field(repr=False)
    preview: str


def prepare_command(
    spec: CommandSpec,
    values: Mapping[str, object],
    changed: AbstractSet[str],
    *,
    platform_name: str | None = None,
) -> PreparedCommand:
    argv = list(spec.path)
    masked = list(spec.path)
    for param in spec.params:
        _append_parameter(argv, masked, param, values, changed)
    return PreparedCommand(
        argv=tuple(argv),
        preview=_format_preview(("reviewer", *masked), platform_name),
    )
```

Никакой `shell=True`. Неизменённые option defaults не сериализовать. Для custom ParamType ранняя
validation отсутствует; raw string попадает в argv и валидируется Click после confirm.

- [ ] **Step 9: Проверить Task 2**

Run:

```bash
.venv/bin/pytest tests/launcher/test_catalog.py tests/launcher/test_command.py -q
.venv/bin/ruff check reviewer/launcher tests/launcher
```

Expected: PASS, lint clean.

- [ ] **Step 10: Commit**

```bash
git add reviewer/launcher tests/launcher
git commit -m "feat(cli): построить каталог и argv для launcher"
```

---

### Task 3: Read-only version service без изменения прямого update

**Files:**
- Create: `reviewer/versioning.py`
- Modify: `reviewer/entrypoints/cli.py:943-1025`
- Create: `tests/test_versioning.py`
- Create: `tests/entrypoints/test_update_command.py`

**Interfaces:**
- Consumes: `importlib.metadata`, `shutil.which`, `subprocess.run`, `urllib.request.urlopen`.
- Produces: `detect_installation(*, distribution: object | None = None, which: Callable[[str], str | None] = shutil.which, run: Callable = subprocess.run) -> InstallationInfo`;
  `check_latest(info: InstallationInfo, *, opener: Callable = urllib.request.urlopen, timeout: int = 10) -> VersionCheck`;
  `upgrade_uv_tool(info: InstallationInfo, *, run: Callable = subprocess.run) -> UpgradeResult`.

- [ ] **Step 1: Написать failing pure-service tests**

```python
class _Distribution:
    version = "0.4.0"

    def __init__(self, *, editable: bool) -> None:
        self._editable = editable

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return json.dumps({"dir_info": {"editable": self._editable}})


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class _Opener:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.timeout: int | None = None

    def __call__(self, request, *, timeout: int):
        self.timeout = timeout
        return _Response(self.payload)


def test_detects_editable_without_running_uv_tool_list():
    run = Mock()
    info = detect_installation(
        distribution=_Distribution(editable=True),
        which=lambda name: "/usr/bin/uv",
        run=run,
    )
    assert info.mode is InstallMode.EDITABLE
    assert info.current == "0.4.0"
    run.assert_not_called()


def test_check_latest_is_read_only_and_uses_timeout():
    opener = _Opener({"info": {"version": "0.5.0"}})
    result = check_latest(
        InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv"),
        opener=opener,
        timeout=5,
    )
    assert result.latest == "0.5.0"
    assert result.update_available is True
    assert opener.timeout == 5
```

Добавить uv-tool/uvx detection, PyPI failure (`latest=None`) и comparison для `0.4.0`,
`0.4.1`, `0.5.0`.

- [ ] **Step 2: Запустить RED service**

Run:

```bash
.venv/bin/pytest tests/test_versioning.py -q
```

Expected: import FAIL для `reviewer.versioning`.

- [ ] **Step 3: Реализовать typed version service**

```python
class InstallMode(StrEnum):
    EDITABLE = "editable"
    UV_TOOL = "uv_tool"
    UVX = "uvx"


@dataclass(frozen=True)
class InstallationInfo:
    mode: InstallMode
    current: str
    uv_executable: str | None


@dataclass(frozen=True)
class VersionCheck:
    installation: InstallationInfo
    latest: str | None
    update_available: bool


@dataclass(frozen=True)
class UpgradeResult:
    returncode: int
    stderr: str
```

Dependency functions инъектируются keyword-only для unit tests. `detect_installation` выполняет
только read-only `uv tool list`; `check_latest` только HTTP GET. Mutation находится отдельно в
`upgrade_uv_tool`.

- [ ] **Step 4: Проверить GREEN service**

Run:

```bash
.venv/bin/pytest tests/test_versioning.py -q
```

Expected: PASS.

- [ ] **Step 5: Зафиксировать failing output contract текущего `reviewer update`**

```python
def test_update_uvx_current_preserves_output(monkeypatch):
    monkeypatch.setattr(
        cli_mod,
        "detect_installation",
        lambda: InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv"),
    )
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda info: VersionCheck(info, "0.4.0", False),
    )

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: uvx (временная) | Версия: 0.4.0\n"
        "Версия актуальна: 0.4.0.\n"
        "MCP-сервер обновляется автоматически — в конфиге клиента прописан @latest.\n"
    )
```

Добавить exact-output tests для editable, network failure, uv-tool upgrade success/failure и
uvx с доступной новой версией.

- [ ] **Step 6: Запустить RED CLI refactor**

Run:

```bash
.venv/bin/pytest tests/entrypoints/test_update_command.py -q
```

Expected: FAIL на monkeypatch/import новых service functions.

- [ ] **Step 7: Перевести Click callback на service с прежним formatter**

Импортировать `detect_installation`, `check_latest`, `upgrade_uv_tool` на уровне
`reviewer.entrypoints.cli`, удалить вложенные helpers из `update`, но оставить строки и порядок
вывода byte-for-byte. Direct `update` по-прежнему сам выполняет upgrade в `UV_TOOL` mode.

- [ ] **Step 8: Проверить Task 3**

Run:

```bash
.venv/bin/pytest tests/test_versioning.py tests/entrypoints/test_update_command.py -q
.venv/bin/pytest tests/entrypoints/test_cli.py tests/install/test_install_wizard.py -q
.venv/bin/ruff check reviewer/versioning.py reviewer/entrypoints/cli.py tests/test_versioning.py tests/entrypoints/test_update_command.py
```

Expected: PASS, existing CLI/init tests green.

- [ ] **Step 9: Commit**

```bash
git add reviewer/versioning.py reviewer/entrypoints/cli.py tests/test_versioning.py tests/entrypoints/test_update_command.py
git commit -m "refactor(cli): выделить сервис проверки обновлений"
```

---

### Task 4: prompt_toolkit palette, forms и explicit update flow

**Files:**
- Modify: `pyproject.toml:35-53`
- Create: `reviewer/launcher/controller.py`
- Create: `reviewer/launcher/app.py`
- Create: `tests/launcher/test_controller.py`
- Create: `tests/launcher/test_app.py`

**Interfaces:**
- Consumes: `build_catalog`, `prepare_command`, `check_latest`, `LauncherResult`.
- Produces: `LauncherController(commands: tuple[CommandSpec, ...])`;
  `run_launcher(*, commands: tuple[CommandSpec, ...] | None = None, input: Input | None = None, output: Output | None = None) -> LauncherResult`.

- [ ] **Step 1: Написать failing controller state tests**

```python
def _spec(name: str) -> CommandSpec:
    return next(item for item in build_catalog(cli) if item.path == (name,))


def test_command_requires_details_then_preview_then_confirm():
    controller = LauncherController((_spec("status"),))
    controller.open_selected()
    assert controller.screen is Screen.DETAILS

    controller.set_value("repo_tag", "a/x")
    controller.open_preview()
    assert controller.screen is Screen.PREVIEW
    assert controller.result is None

    controller.confirm()
    assert controller.result.argv == ("status", "--repo", "a/x")


def test_escape_never_executes_command():
    controller = LauncherController((_spec("gc"),))
    controller.open_selected()
    controller.back()
    controller.cancel()
    assert controller.result == LauncherResult(None, 0)
```

Добавить search/filter ordering, required-field error, boolean toggle, advanced visibility,
secret preview и `Ctrl+C → LauncherResult(None, 130)`.

- [ ] **Step 2: Запустить RED controller**

Run:

```bash
.venv/bin/pytest tests/launcher/test_controller.py -q
```

Expected: import FAIL для `reviewer.launcher.controller`.

- [ ] **Step 3: Реализовать pure state/controller**

```python
class Screen(StrEnum):
    PALETTE = "palette"
    DETAILS = "details"
    PREVIEW = "preview"
    UPDATE_RESULT = "update_result"


_TRANSITIONS = {
    (Screen.PALETTE, "open"): Screen.DETAILS,
    (Screen.DETAILS, "preview"): Screen.PREVIEW,
    (Screen.PREVIEW, "back"): Screen.DETAILS,
    (Screen.DETAILS, "back"): Screen.PALETTE,
}
```

Validation вызывает built-in Click types только через helper с explicit allowlist; custom types
defer to final Click parse. Controller не импортирует prompt_toolkit.
`LauncherController` реализует exact public methods `set_query(query)`, `move(delta)`,
`open_selected()`, `set_value(name, value)`, `toggle_advanced()`, `open_preview()`, `confirm()`,
`back()` и `cancel(exit_code=0)`; неизвестный transition не меняет state и не создаёт result.

- [ ] **Step 4: Проверить GREEN controller**

Run:

```bash
.venv/bin/pytest tests/launcher/test_controller.py -q
```

Expected: PASS.

- [ ] **Step 5: Написать failing prompt_toolkit smoke tests**

```python
def _status_spec() -> CommandSpec:
    return next(item for item in build_catalog(cli) if item.path == ("status",))


def test_escape_from_palette_returns_clean_cancel():
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\x1b")
        result = run_launcher(
            commands=(_status_spec(),),
            input=pipe,
            output=DummyOutput(),
        )
    assert result == LauncherResult(None, 0)


def test_prompt_toolkit_is_not_imported_by_models_or_catalog():
    code = (
        "import sys; "
        "import reviewer.launcher.models, reviewer.launcher.catalog; "
        "assert 'prompt_toolkit' not in sys.modules"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)
```

Второй test запускать с корректным installed/editable `PYTHONPATH` без `-I`, если isolated mode не
видит checkout; требование — fresh interpreter, а не конкретный flag.

- [ ] **Step 6: Запустить RED app**

Run:

```bash
.venv/bin/pytest tests/launcher/test_app.py -q
```

Expected: import FAIL для `reviewer.launcher.app`.

- [ ] **Step 7: Добавить core dependency и реализовать compact palette**

Добавить в `dependencies`:

```toml
"prompt_toolkit>=3.0,<4",
```

`run_launcher` принимает injectable `commands`, `input`, `output` только для tests; production
defaults строят catalog из `cli`. UI использует одну `prompt_toolkit.Application` и динамический
Layout:

```python
def run_launcher(
    *,
    commands: tuple[CommandSpec, ...] | None = None,
    input: Input | None = None,
    output: Output | None = None,
) -> LauncherResult:
    controller = LauncherController(commands or build_catalog(cli))
    application = Application(
        layout=Layout(_root_container(controller)),
        key_bindings=_bindings(controller),
        full_screen=True,
        input=input,
        output=output,
    )
    return application.run()
```

Palette показывает query, список и description; details — basic/advanced поля и effects;
preview — masked command. У narrow terminal блоки складываются вертикально. UI не пишет control
sequences после `Application.run()` и не запускает Click.

- [ ] **Step 8: Реализовать explicit update screen**

При `special_action == "check_update"` первый explicit action запускает `detect_installation` и
`check_latest(..., timeout=5)` через prompt_toolkit executor, показывает progress и result. До
этого PyPI не вызывается. Кнопка «Обновить» доступна только для новой версии в `UV_TOOL` mode и
возвращает `LauncherResult(("update",), 0)`; uvx/editable показывают инструкции без mutation.

Tests инъектируют fake version checker и утверждают: startup/cancel не вызывают его, check вызывает
ровно один раз, upgrade result появляется только после отдельного confirm.

- [ ] **Step 9: Проверить Task 4**

Run:

```bash
uv sync --extra dev
.venv/bin/pytest tests/launcher -q
.venv/bin/ruff check reviewer/launcher tests/launcher
```

Expected: PASS, no terminal snapshot warnings.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml reviewer/launcher tests/launcher
git commit -m "feat(cli): добавить интерактивную command palette"
```

---

### Task 5: Public entry point и regression-контракты прямого CLI

**Files:**
- Modify: `pyproject.toml:74-76`
- Create: `tests/entrypoints/test_launcher_contract.py`
- Modify: `tests/services/test_status.py`

**Interfaces:**
- Consumes: `reviewer.entrypoints.launcher:main`, существующие Click tests/fakes.
- Produces: публичный console script `reviewer = "reviewer.entrypoints.launcher:main"`.

- [ ] **Step 1: Написать failing installed entry-point test**

```python
def test_installed_reviewer_entry_point_loads_launcher():
    scripts = {
        item.name: item
        for item in importlib.metadata.entry_points(group="console_scripts")
        if item.name in {"reviewer", "reviewer-mcp"}
    }
    assert scripts["reviewer"].value == "reviewer.entrypoints.launcher:main"
    assert scripts["reviewer"].load() is launcher.main
    assert scripts["reviewer-mcp"].value == "reviewer.entrypoints.mcp_server:main"
```

- [ ] **Step 2: Запустить RED metadata**

Run:

```bash
.venv/bin/pytest tests/entrypoints/test_launcher_contract.py::test_installed_reviewer_entry_point_loads_launcher -q
```

Expected: FAIL: installed editable metadata ещё указывает на `reviewer.entrypoints.cli:cli`.

- [ ] **Step 3: Переключить только reviewer entry point**

```toml
[project.scripts]
reviewer = "reviewer.entrypoints.launcher:main"
reviewer-mcp = "reviewer.entrypoints.mcp_server:main"
```

Обновить editable metadata и повторить focused test:

```bash
uv sync --extra dev --extra web
.venv/bin/pytest tests/entrypoints/test_launcher_contract.py::test_installed_reviewer_entry_point_loads_launcher -q
```

- [ ] **Step 4: Написать failing exact delegation tests**

```python
@pytest.mark.parametrize(
    "argv",
    [
        ("--help",),
        ("check",),
        ("status", ".", "--repo", "a/x", "--json"),
        ("init", "--yes", "--dry-run"),
        ("install", "codex", "--dry-run"),
        ("does-not-exist",),
    ],
)
def test_direct_contract_never_enters_tui(monkeypatch, argv):
    tui = Mock(side_effect=AssertionError("TUI вызван на direct route"))
    cli_call = Mock()
    monkeypatch.setattr(launcher, "_run_tui", tui)
    monkeypatch.setattr(launcher, "_run_cli", cli_call)

    launcher.main(argv)

    cli_call.assert_called_once_with(argv)
    tui.assert_not_called()
```

Добавить integration-style helper и comparisons:

```python
def _invoke_launcher(argv: tuple[str, ...], capsys) -> tuple[int, str, str]:
    try:
        launcher.main(argv)
    except SystemExit as error:
        code = int(error.code or 0)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("argv", [("--help",), ("does-not-exist",)])
def test_launcher_output_matches_direct_click(argv, capsys):
    direct = CliRunner().invoke(cli, list(argv))
    actual = _invoke_launcher(argv, capsys)
    assert actual == (direct.exit_code, direct.stdout, direct.stderr)


def test_launcher_status_json_is_clean(monkeypatch, capsys, status_report):
    monkeypatch.setattr(cli_mod, "build_status_report", lambda *args, **kwargs: status_report)
    monkeypatch.setattr(cli_mod, "ChunkStore", MagicMock())
    monkeypatch.setattr(cli_mod, "GraphStore", MagicMock())
    code, stdout, stderr = _invoke_launcher(
        ("status", ".", "--repo", "a/x", "--json"),
        capsys,
    )
    assert code == 0
    assert stderr == ""
    assert json.loads(stdout)["repo"] == "a/x"
    assert "\x1b" not in stdout
```

`status_report` повторяет существующий `RepoStatus` fixture из `tests/services/test_status.py`
и содержит одну свежую ветку `main`.

- [ ] **Step 5: Запустить RED contracts**

Run:

```bash
.venv/bin/pytest tests/entrypoints/test_launcher_contract.py tests/services/test_status.py -q
```

Expected: FAIL до test helpers/entry point integration.

- [ ] **Step 6: Добавить no-args TTY/non-TTY и fresh-process import tests**

```python
def test_non_tty_no_args_delegates_existing_click(monkeypatch):
    monkeypatch.setattr(
        launcher,
        "should_use_tui",
        lambda *args, **kwargs: False,
    )
    direct = Mock()
    monkeypatch.setattr(launcher, "_run_cli", direct)
    launcher.main([])
    direct.assert_called_once_with(())


def test_mcp_import_does_not_load_launcher_or_prompt_toolkit():
    code = (
        "import sys; "
        "import reviewer.entrypoints.mcp_server; "
        "assert 'reviewer.entrypoints.launcher' not in sys.modules; "
        "assert 'prompt_toolkit' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
```

- [ ] **Step 7: Проверить public contract suite**

Run:

```bash
.venv/bin/pytest \
  tests/entrypoints/test_launcher.py \
  tests/entrypoints/test_launcher_contract.py \
  tests/entrypoints/test_cli.py \
  tests/services/test_status.py \
  tests/install/test_install_wizard.py \
  tests/install/test_install.py \
  -q
.venv/bin/ruff check reviewer/entrypoints tests/entrypoints/test_launcher.py tests/entrypoints/test_launcher_contract.py
```

Expected: PASS; `status --json` остаётся valid JSON.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml tests/entrypoints/test_launcher_contract.py tests/services/test_status.py
git commit -m "test(cli): закрепить совместимость launcher"
```

---

### Task 6: Wheel/uv distribution gate, cross-platform CI и README

**Files:**
- Create: `scripts/verify_launcher_distribution.py`
- Create: `tests/scripts/test_verify_launcher_distribution.py`
- Modify: `.github/workflows/tests.yml`
- Modify: `README.md:221-265`
- Modify: `README.md:618-657`

**Interfaces:**
- Consumes: собранный wheel, `uv`, `uvx`, console scripts.
- Produces: `verify_distribution(wheel_dir: Path, *, runner=...) -> None`, matrix job `launcher-platform`.

- [ ] **Step 1: Написать failing distribution-helper tests**

```python
def _recording_runner(calls: list[Command]):
    def run(command: Command) -> None:
        calls.append(command)

    return run


def test_distribution_check_uses_isolated_uv_dirs_and_outside_checkout(tmp_path):
    wheel_dir = tmp_path / "dist"
    wheel_dir.mkdir()
    wheel = wheel_dir / "rag_reviewer-0.4.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    calls = []

    verify_distribution(wheel_dir, runner=_recording_runner(calls))

    install = calls[0]
    assert install.argv[:4] == ("uv", "tool", "install", "--force")
    assert install.argv[-1] == str(wheel.resolve())
    assert install.env["UV_TOOL_DIR"].startswith(str(tmp_path))
    assert install.env["UV_TOOL_BIN_DIR"].startswith(str(tmp_path))
    assert all(call.cwd != Path.cwd() for call in calls)
    assert any(call.argv[:2] == ("uvx", "--from") for call in calls)
```

Добавить Windows executable suffix test и error, если в `dist/` не ровно один wheel.

- [ ] **Step 2: Запустить RED helper**

Run:

```bash
.venv/bin/pytest tests/scripts/test_verify_launcher_distribution.py -q
```

Expected: import/file FAIL для `scripts.verify_launcher_distribution`.

- [ ] **Step 3: Реализовать cross-platform verifier**

```python
@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]


def run_command(command: Command) -> None:
    subprocess.run(
        list(command.argv),
        cwd=command.cwd,
        env=dict(command.env),
        check=True,
    )


def verify_distribution(
    wheel_dir: Path,
    *,
    runner: Callable[[Command], None] = run_command,
) -> None:
    wheels = sorted(wheel_dir.resolve().glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"ожидался один wheel, найдено: {len(wheels)}")
    wheel = wheels[0]
    with TemporaryDirectory(prefix="reviewer-launcher-") as raw:
        root = Path(raw)
        tool_dir = root / "tools"
        bin_dir = root / "bin"
        outside = root / "outside-checkout"
        outside.mkdir()
        env = {
            **os.environ,
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "UV_CACHE_DIR": str(root / "cache"),
        }
        runner(Command(("uv", "tool", "install", "--force", str(wheel)), outside, env))
        executable = bin_dir / ("reviewer.exe" if os.name == "nt" else "reviewer")
        runner(Command((str(executable), "--help"), outside, env))
        runner(Command(("uvx", "--from", str(wheel), "reviewer", "--help"), outside, env))
```

Production runner использует `subprocess.run(list(argv), cwd=..., env=..., check=True)` без shell.
CLI `main()` принимает `dist` path и печатает только подтверждённые steps.

- [ ] **Step 4: Проверить GREEN helper**

Run:

```bash
.venv/bin/pytest tests/scripts/test_verify_launcher_distribution.py -q
.venv/bin/ruff check scripts/verify_launcher_distribution.py tests/scripts/test_verify_launcher_distribution.py
```

Expected: PASS.

- [ ] **Step 5: Добавить launcher-platform CI job**

```yaml
  launcher-platform:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0
      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]" uv
      - run: pytest tests/entrypoints/test_launcher.py tests/entrypoints/test_launcher_contract.py tests/launcher -q
      - run: uv build
      - run: python scripts/verify_launcher_distribution.py dist
```

Не дублировать полный suite на трёх ОС: он остаётся в существующем Linux job.

- [ ] **Step 6: Обновить README с единственным uv installation flow**

В Quick setup сохранить:

```bash
uv tool install rag-reviewer
reviewer
```

Добавить рядом:

```bash
# Временный запуск без постоянной установки
uvx --from rag-reviewer@latest reviewer
```

Текст явно говорит: launcher работает из любого каталога, checkout/clone не нужен; без аргументов
в TTY открывается palette; `reviewer check`, `reviewer status --json`, pipe/CI и
`reviewer-mcp` идут по прежнему прямому пути; обновления проверяются только из явного действия.

В CLI reference добавить keyboard shortcuts `↑/↓`, search, `Enter`, `Esc`, `Ctrl+C`, masked
preview и пример `reviewer status --repo owner/name --json`.

- [ ] **Step 7: Запустить локальные release checks**

Run:

```bash
.venv/bin/pytest \
  tests/scripts/test_verify_launcher_distribution.py \
  tests/entrypoints/test_launcher.py \
  tests/entrypoints/test_launcher_contract.py \
  tests/launcher \
  -q
.venv/bin/ruff check reviewer scripts/verify_launcher_distribution.py tests
uv build
python scripts/verify_launcher_distribution.py dist
```

Expected: tests/lint/build PASS; verifier подтверждает `uv tool install` и `uvx --from` из
временного каталога вне checkout.

- [ ] **Step 8: Commit**

```bash
git add scripts/verify_launcher_distribution.py tests/scripts/test_verify_launcher_distribution.py .github/workflows/tests.yml README.md
git commit -m "ci(cli): проверить глобальную uv-установку launcher"
```

---

## Final Verification

После всех task commits controller выполняет:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
uv build
python scripts/verify_launcher_distribution.py dist
git diff --check
```

Дополнительно вручную в pseudo-TTY:

```bash
reviewer
```

Проверить search → `status` → params → masked preview → `Esc`, затем повторить и подтвердить
read-only `status`. Прямые команды:

```bash
reviewer --help
reviewer status . --repo mimfort/rag_for_git --branch dev --json
```

должны завершаться без TUI/ANSI до собственного вывода.
