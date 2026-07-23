# Extensible Board Providers and Jira Cloud Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить закрытый набор YouGile/YouTrack на явный registry полного provider-контракта, сохранить их функциональный паритет и добавить Jira Cloud REST API v3, generic MCP/config/skills и безопасный credential setup.

**Architecture:** Каждый adapter экспортирует immutable `BoardProviderSpec`; центральный registry явно регистрирует specs и создаёт provider из server-side credentials и immutable runtime options. `SyncService`, MCP lifecycle, installer и skills работают только с нормализованными registry metadata, targets и options. Legacy env и `.review.yml` поля читаются одним compatibility-слоем в течение одного релиза, но новые interfaces и docs их не генерируют.

**Tech Stack:** Python 3.11–3.13, dataclasses/typing Protocol, Pydantic Settings, `python-dotenv`, `httpx`, Click, pytest, pytest-socket, Ruff, Jira Cloud REST API v3/ADF.

## Global Constraints

- Работать test-first: для каждого behavior сначала добавить failing test, увидеть ожидаемый fail, затем писать минимальную реализацию.
- Default unit suite остаётся offline; HTTP тестировать только через `httpx.MockTransport` или fake client. Live smokes маркировать `integration`.
- Generic modules не содержат сравнений с `"yougile"`, `"youtrack"` или `"jira"` и не импортируют конкретные provider-классы.
- Credentials читаются только server-side. Secret values запрещены в options, return values, warnings, exception text/repr и logs.
- Registry считается единственной точкой объявления поддерживаемого board type. Частичный provider зарегистрировать нельзя.
- Сохранить watermark `tasks:<type>:<board>`, полный enumerate для purge, write-through после create/finish и текущий `TaskBrief` storage format.
- Jira v1 поддерживает только Cloud REST API v3 с direct site URL и Basic auth `email:unscoped-api-token`. Server/DC, OAuth и scoped-token gateway не входят.
- YouGile acquisition password живёт только в локальной переменной hook, очищается в `finally` и никогда не попадает в возвращаемый state.
- Не менять и не добавлять в коммиты посторонние untracked файлы рабочего дерева.

## Target Interfaces

Эти сигнатуры являются общей точкой согласования для всех задач плана:

```python
# reviewer/tasks/boards/base.py
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

class TaskBoardProvider(Protocol):
    board_type: str

    def validate_connection(self, project: str | None = None) -> dict: ...
    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]: ...
    def normalize(self, raw: RawTask) -> dict: ...
    def normalize_meta(self, raw: RawTask) -> dict: ...
    def fetch_one(self, key: str) -> RawTask | None: ...
    def list_targets(self, project: str | None) -> dict: ...
    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict: ...
    def finish(
        self,
        key: str,
        pr_url: str,
        *,
        note: str | None = None,
        mark_done: bool = True,
        target: str | None = None,
    ) -> dict: ...
    def close(self) -> None: ...
```

```python
# reviewer/tasks/boards/registry.py
@dataclass(frozen=True)
class CredentialFieldSpec:
    env: str
    label: str
    secret: bool = False
    required: bool = True
    default: str = ""
    aliases: tuple[str, ...] = ()

@dataclass(frozen=True)
class ProviderOptionSpec:
    key: str
    label: str
    required_for: tuple[Literal["sync", "create", "finish"], ...] = ()

@dataclass(frozen=True)
class ProviderSetupSpec:
    label: str
    help_url: str
    help_text: str
    acquisition: Callable[["SetupIO"], dict[str, str]] | None = None

@dataclass(frozen=True)
class ProviderBuildContext:
    credentials: Mapping[str, str]
    options: Mapping[str, JsonValue]
    key_pattern: str
    url_template: str
    attachment_max_bytes: int
    attachment_timeout: float
    attachment_store_chars: int

@dataclass(frozen=True)
class BoardProviderSpec:
    board_type: str
    factory: Callable[[ProviderBuildContext], TaskBoardProvider]
    credential_fields: tuple[CredentialFieldSpec, ...]
    setup: ProviderSetupSpec
    option_fields: tuple[ProviderOptionSpec, ...] = ()
    default_api_base: str = ""
    create_target_label: str = "Create target"
    done_target_label: str = "Done target"

class BoardProviderError(RuntimeError):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        hint: str = "",
        retryable: bool = False,
        secrets: Collection[str] = (),
    ) -> None: ...
```

```python
# public MCP/service shape
def sync_board(
    board: str | None = None,
    limit: int | None = None,
    purge_orphaned: bool = False,
    keep_with_prs: bool = True,
    board_type: str | None = None,
    provider_options: dict[str, JsonValue] | None = None,
    force_renormalize: bool = False,
) -> dict: ...

def create_task(
    title: str,
    problem: str = "",
    steps: list[str] | None = None,
    criteria: list[str] | None = None,
    context: str | None = None,
    board_type: str | None = None,
    project: str | None = None,
    target: str | None = None,
    provider_options: dict[str, JsonValue] | None = None,
) -> dict: ...

def get_board_targets(
    board_type: str | None = None,
    project: str | None = None,
    provider_options: dict[str, JsonValue] | None = None,
) -> dict: ...

def finish_task(
    key: str,
    pr_url: str,
    note: str | None = None,
    mark_done: bool = True,
    board_type: str | None = None,
    target: str | None = None,
    provider_options: dict[str, JsonValue] | None = None,
) -> dict: ...
```

---

### Task 1: Ввести безопасные registry, credential source и provider errors

**Files:**

- Create: `reviewer/tasks/boards/registry.py`
- Create: `reviewer/tasks/boards/errors.py`
- Create: `reviewer/tasks/boards/http.py`
- Create: `reviewer/config/provider_credentials.py`
- Modify: `reviewer/tasks/boards/base.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/tasks/boards/test_registry.py`
- Create: `tests/config/test_provider_credentials.py`
- Create: `tests/tasks/boards/test_board_errors.py`
- Create: `tests/tasks/boards/test_board_http.py`

**Interfaces:**

- `BoardProviderRegistry.register/get/registered_types/create`
- `ProviderCredentialSource.resolve/is_configured/configured_types/secret_values`
- `BoardProviderError(category, message, hint, retryable)`
- `sanitize_provider_text(value, secrets)`
- `BoardHttpClient.request_json(method, path, *, operation)`

- [ ] Написать registry tests: стабильный порядок, duplicate/empty type, несовместимые повторные env declarations, неизвестный option, secret env key в options, неполный runtime provider.

```python
def test_registry_rejects_secret_option_and_incomplete_provider():
    registry = BoardProviderRegistry()
    registry.register(fake_spec(secret_env="FAKE_TOKEN"))
    with pytest.raises(ValueError, match="credentials must not be provider options"):
        registry.create(
            "fake",
            credentials={"FAKE_TOKEN": "secret"},
            options={"FAKE_TOKEN": "secret"},
            build_defaults=BUILD_DEFAULTS,
        )
    registry.register(fake_spec(factory=lambda _: object(), board_type="broken"))
    with pytest.raises(TypeError, match="validate_connection"):
        registry.create(
            "broken",
            credentials={"FAKE_TOKEN": "x"},
            options={},
            build_defaults=BUILD_DEFAULTS,
        )
```

- [ ] Запустить `uv run pytest tests/tasks/boards/test_registry.py -q`; ожидать fail из-за отсутствующего `reviewer.tasks.boards.registry`.
- [ ] Добавить типы из раздела Target Interfaces и `BoardProviderRegistry`. При `register()` проверять metadata немедленно, а полный runtime contract — на результате `factory` в `create()` через `inspect.getattr_static` по списку обязательных методов.
- [ ] Написать credential tests на приоритет `os.environ > resolved .env > default`, legacy alias, required-set и отсутствие secret values в safe metadata.

```python
def test_process_env_wins_and_legacy_alias_is_supported(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TASK_BOARD_API_KEY=legacy-file\n", encoding="utf-8")
    monkeypatch.setenv("YOUGILE_API_KEY", "process-value")
    source = ProviderCredentialSource(env_file=env_file)
    resolved = source.resolve(YOUGILE_SPEC)
    assert resolved.values["YOUGILE_API_KEY"] == "process-value"
    assert resolved.safe_metadata == {"configured": True, "missing": []}
    assert "process-value" not in repr(resolved.safe_metadata)
```

- [ ] Запустить `uv run pytest tests/config/test_provider_credentials.py -q`; ожидать fail из-за отсутствующего source.
- [ ] Добавить прямую dependency `"python-dotenv>=1.0"` в `pyproject.toml`, выполнить `uv lock`, затем реализовать source через `dotenv_values(_resolve_env_file())`; не читать arbitrary cwd повторно, если передан явный `env_file`.
- [ ] Написать error/redaction tests, включая secret в URL query, headers-подобном тексте, exception chain, `str`, `repr` и captured logs.

```python
def test_board_error_never_exposes_secret(caplog):
    error = BoardProviderError(
        "authentication",
        "Jira rejected token top-secret",
        hint="rotate top-secret",
        retryable=False,
        secrets={"top-secret"},
    )
    logging.getLogger("reviewer.test").warning("%s", error)
    rendered = f"{error!s} {error!r} {caplog.text}"
    assert "top-secret" not in rendered
    assert "[REDACTED]" in rendered
```

- [ ] Запустить `uv run pytest tests/tasks/boards/test_board_errors.py -q`; ожидать fail, затем реализовать allowlisted categories, safe properties и longest-first literal redaction.
- [ ] Во всех adapter wrappers поднимать safe error через `raise BoardProviderError(...) from None`, чтобы traceback boundary не печатал исходный transport exception с request headers/body.
- [ ] Написать `BoardHttpClient` tests: read 429/5xx учитывает `Retry-After` и bounded attempts; 401/403 не повторяются; `operation="write"` никогда автоматически не повторяется после response/transport uncertainty.
- [ ] Запустить `uv run pytest tests/tasks/boards/test_board_http.py -q`; ожидать import fail, затем реализовать transport wrapper с injectable sleeper и safe `BoardProviderError`.
- [ ] Расширить `TaskBoardProvider` полным контрактом из Target Interfaces и добавить `JsonValue`; пока не менять существующие adapter implementations.
- [ ] Запустить `uv run pytest tests/tasks/boards/test_registry.py tests/config/test_provider_credentials.py tests/tasks/boards/test_board_errors.py tests/tasks/boards/test_board_http.py -q`; ожидать green.
- [ ] Выполнить `uv run ruff check reviewer/tasks/boards/registry.py reviewer/tasks/boards/errors.py reviewer/tasks/boards/http.py reviewer/config/provider_credentials.py reviewer/tasks/boards/base.py tests/tasks/boards/test_registry.py tests/config/test_provider_credentials.py tests/tasks/boards/test_board_errors.py tests/tasks/boards/test_board_http.py`.
- [ ] Закоммитить только файлы задачи: `git commit -m "feat: добавить реестр провайдеров досок (PRI-215)"`.

---

### Task 2: Зарегистрировать YouGile и YouTrack без ветвлений в Settings/factory

**Files:**

- Modify: `reviewer/tasks/boards/yougile.py`
- Modify: `reviewer/tasks/boards/youtrack.py`
- Modify: `reviewer/tasks/boards/registry.py`
- Modify: `reviewer/tasks/boards/__init__.py`
- Modify: `reviewer/config/settings.py`
- Modify: `reviewer/app.py`
- Modify: `tests/tasks/boards/test_base.py`
- Modify: `tests/config/test_settings.py`
- Add fixtures: `tests/tasks/boards/provider_fakes.py`

**Interfaces:**

```python
def default_board_registry() -> BoardProviderRegistry:
    registry = BoardProviderRegistry()
    registry.register(yougile_provider_spec())
    registry.register(youtrack_provider_spec())
    return registry

def make_board_provider(
    settings: Settings,
    type_: str,
    *,
    provider_options: Mapping[str, JsonValue] | None = None,
) -> TaskBoardProvider | None: ...
```

- [ ] Переписать factory tests: unknown type остаётся `None`, configured types берутся из registry specs, fake registry provider создаётся без изменения `Settings`, а generic modules не содержат concrete type literals.

```python
def test_factory_uses_injected_registry_for_new_provider():
    registry = BoardProviderRegistry([fake_provider_spec()])
    settings = Settings(_env_file=None)
    provider = make_board_provider(
        settings,
        "fake",
        registry=registry,
        credential_source=ProviderCredentialSource(values={"FAKE_TOKEN": "x"}),
    )
    assert provider.board_type == "fake"
```

- [ ] Запустить `uv run pytest tests/tasks/boards/test_base.py tests/config/test_settings.py -q`; ожидать fail на новых registry arguments/configured behavior.
- [ ] Добавить в `yougile.py` `provider_spec()` с `YOUGILE_API_KEY`, alias `TASK_BOARD_API_KEY`, optional `YOUGILE_API_BASE`, alias `TASK_BOARD_API_BASE`, default `https://yougile.com/api-v2`; factory получает только `ProviderBuildContext`.
- [ ] Добавить в `youtrack.py` `provider_spec()` с required `YOUTRACK_TOKEN`/`YOUTRACK_BASE_URL` и option `status_field`; factory фиксирует `status_field` при создании.
- [ ] Удалить `_BOARD_API_BASE_DEFAULTS`, concrete branches из `Settings.board_creds`, `Settings.configured_board_types` и `boards.make_board_provider`; compatibility methods делегируют registry/source.

```python
def configured_board_types(self) -> list[str]:
    return default_board_registry().configured_types(
        ProviderCredentialSource.from_settings(self)
    )
```

- [ ] Сделать `default_board_registry()` cached и с локальными явными imports двух существующих `provider_spec`, чтобы module initialization не образовал цикл; Task 7 добавит третью явную строку Jira, fake production entry не создавать.
- [ ] Обновить `make_board_providers` и `reviewer/app.py` на один credential source и registry; закрывать уже созданные providers при ошибке создания следующего.
- [ ] Запустить `uv run pytest tests/tasks/boards/test_base.py tests/config/test_settings.py -q`; ожидать green.
- [ ] Выполнить literal guard:

```bash
rg -n 'if .*board_type.*(yougile|youtrack)|if .*type_.*(yougile|youtrack)' \
  reviewer/tasks/boards/__init__.py reviewer/config/settings.py reviewer/app.py
```

Ожидание: exit 1, совпадений нет.

- [ ] Запустить существующие adapter tests: `uv run pytest tests/tasks/boards/test_yougile_*.py tests/tasks/boards/test_youtrack_*.py -q`.
- [ ] Закоммитить: `git commit -m "refactor: зарегистрировать существующие доски (PRI-215)"`.

---

### Task 3: Унифицировать полный provider contract и contract-test suite

**Files:**

- Modify: `reviewer/tasks/boards/base.py`
- Modify: `reviewer/tasks/boards/yougile.py`
- Modify: `reviewer/tasks/boards/youtrack.py`
- Create: `tests/tasks/boards/contract.py`
- Create: `tests/tasks/boards/test_provider_contract.py`
- Modify: `tests/tasks/boards/test_yougile_targets.py`
- Modify: `tests/tasks/boards/test_youtrack_targets.py`
- Modify: `tests/tasks/boards/test_yougile_finish.py`
- Modify: `tests/tasks/boards/test_youtrack_finish.py`
- Modify: `tests/tasks/boards/test_yougile_normalize.py`
- Modify: `tests/tasks/boards/test_youtrack_normalize.py`

**Normalized discovery:**

```python
{
    "targets": [{"id": "done-id", "label": "Done", "purposes": ["create", "done"]}],
    "options": [{
        "key": "status_field",
        "label": "Status field",
        "required_for": ["sync", "create", "finish"],
        "choices": [{"id": "State", "label": "State"}],
    }],
    "warnings": [],
}
```

- [ ] Создать reusable contract mixin/fixture, который принимает factory и deterministic fake transport; тесты должны покрыть все 14 пунктов из design spec, а не только наличие методов.

```python
class ProviderContract:
    provider_factory: Callable[[], TaskBoardProvider]

    def test_fetch_one_matches_iter_mapper(self):
        provider = self.provider_factory()
        raw = next(iter(provider.iter_raw("PRI", None)))
        one = provider.fetch_one(raw.key)
        assert one is not None
        assert dataclasses.asdict(one) == dataclasses.asdict(raw)
```

- [ ] Запустить `uv run pytest tests/tasks/boards/test_provider_contract.py -q`; ожидать failures для `validate_connection`, `list_targets` и новой finish signature.
- [ ] Реализовать `validate_connection(project=None)` у YouGile и YouTrack через минимальные read-only identity/project requests; возвращать только `{status, identity, project, capabilities, warnings}`.
- [ ] Перевести YouGile и YouTrack transport helpers на общий `BoardHttpClient`; сохранить payload/endpoint semantics и добавить provider-specific mapping assertions для 404 и permission failures.
- [ ] Заменить `list_done_targets` на `list_targets` и нормализовать старые columns/status fields. Targets поддерживают resolution по exact `id` или exact `label`; неоднозначный label возвращает warning без выбора.
- [ ] Заменить `finish(...done_state, done_column)` на `finish(...target)` внутри обоих providers. YouGile трактует target как column id/label, YouTrack — как status value при зафиксированном option `status_field`.
- [ ] Удалить `YouTrackBoard.set_status_field`; сохранить immutable `self._status_field` из constructor.
- [ ] Убедиться, что `normalize_meta` не вызывает client: contract fixture должен обнулять HTTP budget перед вызовом.
- [ ] Перенести существующие assertions по HTML/Markdown, links, subtasks, attachments, fallback targets, idempotent PR-link и partial finish в provider-specific fixtures contract suite.
- [ ] Добавить contract assertion `close()` на success/error и secret absence в result/warnings/captured logs.
- [ ] Запустить:

```bash
uv run pytest \
  tests/tasks/boards/test_provider_contract.py \
  tests/tasks/boards/test_yougile_*.py \
  tests/tasks/boards/test_youtrack_*.py -q
```

Ожидание: green и ни одного реального socket request.

- [ ] Выполнить `uv run ruff check reviewer/tasks/boards tests/tasks/boards`.
- [ ] Закоммитить: `git commit -m "refactor: унифицировать контракт досок (PRI-215)"`.

---

### Task 4: Сделать SyncService и MCP lifecycle полностью generic

**Files:**

- Create: `reviewer/tasks/boards/runtime.py`
- Create: `reviewer/config/task_board.py`
- Modify: `reviewer/tasks/sync.py`
- Modify: `reviewer/mcp/service.py`
- Modify: `reviewer/entrypoints/mcp_server.py`
- Modify: `tests/tasks/test_sync.py`
- Modify: `tests/mcp/test_sync_board.py`
- Modify: `tests/mcp/test_create_task.py`
- Modify: `tests/mcp/test_finish_task.py`
- Modify: `tests/mcp/test_get_board_targets.py`
- Modify: `tests/mcp/test_server_tools.py`
- Create: `tests/mcp/test_board_provider_extensibility.py`

**Interfaces:**

```python
@contextmanager
def resolved_provider(
    settings: Settings,
    board_type: str | None,
    provider_options: Mapping[str, JsonValue] | None,
    *,
    registry: BoardProviderRegistry,
    credential_source: ProviderCredentialSource,
) -> Iterator[ResolvedProvider]: ...

@dataclass(frozen=True)
class ResolvedProvider:
    board_type: str
    provider: TaskBoardProvider
    secrets: frozenset[str]
```

- [ ] Обновить SyncService tests: передавать providers, уже созданные с immutable options; удалить test на `set_status_field`; добавить fake provider sync без production change.
- [ ] Запустить `uv run pytest tests/tasks/test_sync.py -q`; ожидать fail, пока `SyncService.run` принимает `status_field`.
- [ ] Удалить `status_field` из `SyncService.run` и concrete mutation loop: options уже провалидированы и зафиксированы в provider до передачи в sync.
- [ ] Для scoped sync создавать provider через MCP resolver и передавать его во временный `SyncService([provider], ...)`; deploy-wide `sync_board` без `board_type` и без options сохраняет старый multi-provider обход.
- [ ] Добавить правило: если настроено несколько providers и переданы `provider_options` без `board_type`, вернуть configuration error — один object options нельзя безопасно применить к разным schemas.
- [ ] Написать lifecycle tests: единственный configured provider выбирается автоматически; create/finish/discovery при нескольких требуют type; deploy-wide sync без options обходит все; unknown/unconfigured дают safe error; provider закрывается при success, provider error и write-through error.
- [ ] Написать fake-provider extensibility test, который через injected registry выполняет sync, discovery, create и finish и проверяет write-through без изменений production registry.

```python
def test_fake_provider_runs_full_mcp_lifecycle(service_with_registry):
    service, fake = service_with_registry(fake_provider_spec())
    assert service.get_board_targets("fake")["targets"]
    assert service.create_task("T", board_type="fake")["reindexed"] is True
    assert service.finish_task("FAKE-1", "https://github/pr/1",
                               board_type="fake")["reindexed"] is True
    assert fake.closed_calls == 3
```

- [ ] Запустить MCP tests из списка Files; ожидать fail на старых signatures и duplicated resolution.
- [ ] Реализовать `resolved_provider` как единственное место type resolution, credential validation, option validation, provider creation, `close()` и final error sanitization.
- [ ] В `reviewer/config/task_board.py` добавить узкий `migrate_legacy_board_args`: new `target/provider_options` имеют приоритет, legacy args преобразуются и возвращают deterministic migration warnings. YAML normalization будет добавлена в Task 5.
- [ ] Вынести `_write_through(provider, key)` в `MCPReviewService`; использовать после create и finish независимо от частичных provider warnings.
- [ ] Перевести `sync_board/create_task/finish_task/get_board_targets` на wrapper и normalized discovery. Ни один handler не вызывает `make_board_provider` напрямую.
- [ ] Изменить MCP tool signatures/docstrings на Target Interfaces. На boundary принимать legacy args только как keyword-only compatibility shim, не рекламировать их в docstrings:

```python
def finish_task(
    key, pr_url, note=None, mark_done=True, board_type=None,
    target=None, provider_options=None, done_state=None,
    status_field=None, done_column=None,
):
    target, provider_options, migration_warnings = migrate_legacy_board_args(
        target=target,
        provider_options=provider_options,
        done_state=done_state,
        status_field=status_field,
        done_column=done_column,
    )
```

- [ ] Выполнить guard:

```bash
rg -n 'yougile|youtrack|jira|status_field|done_state|done_column' \
  reviewer/tasks/sync.py reviewer/tasks/boards/runtime.py
```

Ожидание: exit 1. В `mcp/service.py` допустимы legacy имена только в compatibility signature/helper call, concrete board literals отсутствуют.

- [ ] Запустить:

```bash
uv run pytest tests/tasks/test_sync.py \
  tests/mcp/test_sync_board.py tests/mcp/test_create_task.py \
  tests/mcp/test_finish_task.py tests/mcp/test_get_board_targets.py \
  tests/mcp/test_server_tools.py tests/mcp/test_board_provider_extensibility.py -q
```

Ожидание: green.

- [ ] Закоммитить: `git commit -m "refactor: обобщить lifecycle досок в MCP (PRI-215)"`.

---

### Task 5: Нормализовать `.review.yml` и legacy migration в одном слое

**Files:**

- Modify: `reviewer/config/task_board.py`
- Modify: `reviewer/policy/policy.py`
- Modify: `tests/policy/test_policy.py`
- Create: `tests/config/test_task_board_config.py`
- Modify: `.review.yml`

**Interfaces:**

```python
@dataclass(frozen=True)
class TaskBoardConfig:
    board_type: str | tuple[str, ...] | None
    mcp: str | None
    project: str | None
    key_pattern: str | None
    url_template: str | None
    create_target: str | None
    done_target: str | None
    options: Mapping[str, JsonValue]
    warnings: tuple[str, ...] = ()

def normalize_task_board_config(raw: Mapping[str, object] | None) -> TaskBoardConfig | None: ...
```

- [ ] Написать tests для новой формы, пустого блока, malformed options, legacy mapping и new-wins warning.

```python
def test_new_values_win_over_legacy_with_warning():
    config = normalize_task_board_config({
        "type": "youtrack",
        "done_target": "Done",
        "done_state": "Fixed",
        "options": {"status_field": "Stage"},
        "status_field": "State",
    })
    assert config.done_target == "Done"
    assert config.options == {"status_field": "Stage"}
    assert config.warnings == (
        "task_board.done_state ignored because done_target is set",
        "task_board.status_field ignored because options.status_field is set",
    )
```

- [ ] Запустить `uv run pytest tests/config/test_task_board_config.py tests/policy/test_policy.py -q`; ожидать fail из-за отсутствующего normalizer.
- [ ] Реализовать pure normalizer: `done_column`/`done_state → done_target`, `status_field → options.status_field`; не мутировать input, reject secrets по registry secret env names.
- [ ] Сохранить public `ReviewPolicy.task_board` как plain dict для совместимости callers, но нормализовать в `from_yaml/load`; migration messages хранить в новом `ReviewPolicy.task_board_warnings: list[str]`, а не подмешивать служебный ключ в provider options.
- [ ] Обновить `.review.yml` на `create_target`, `done_target`, `options`; не добавлять credentials и provider-specific comments.
- [ ] Добавить service tests, что policy values проходят в MCP как `target/provider_options`, а legacy config даёт migration warnings ровно один раз.
- [ ] Запустить `uv run pytest tests/config/test_task_board_config.py tests/policy/test_policy.py tests/mcp/test_*task.py tests/mcp/test_sync_board.py -q`; ожидать green.
- [ ] Выполнить `uv run ruff check reviewer/config/task_board.py reviewer/policy/policy.py tests/config/test_task_board_config.py tests/policy/test_policy.py`.
- [ ] Закоммитить: `git commit -m "feat: нормализовать конфиг досок (PRI-215)"`.

---

### Task 6: Реализовать чистый Jira ADF ↔ Markdown converter

**Files:**

- Create: `reviewer/tasks/boards/adf.py`
- Create: `tests/tasks/boards/test_adf.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class AdfConversion:
    value: str | dict
    warnings: tuple[str, ...] = ()

def adf_to_markdown(document: Mapping[str, object] | None) -> AdfConversion: ...
def markdown_to_adf(markdown: str) -> AdfConversion: ...
def adf_contains_link(document: Mapping[str, object] | None, href: str) -> bool: ...
def append_link_paragraph(
    document: Mapping[str, object] | None,
    href: str,
    *,
    label: str,
    note: str | None,
) -> dict: ...
```

- [ ] Написать parameterized ADF→Markdown tests для paragraph/hardBreak, headings, bullet/ordered lists, blockquote, codeBlock, strong/em/code/link marks.
- [ ] Написать canonical task document round-trip test: Markdown после ADF round-trip нормализует только terminal newline, не теряет headings, lists и links.
- [ ] Написать unknown-node test: рекурсивный text сохраняется, warning содержит node type, converter не падает.
- [ ] Написать link identity tests: поиск по `href`, не visible label; append не дублирует существующий href и добавляет optional note отдельным paragraph.
- [ ] Запустить `uv run pytest tests/tasks/boards/test_adf.py -q`; ожидать import fail.
- [ ] Реализовать stdlib-only ADF walker и небольшой parser canonical Markdown subset. Возвращать новый dict и не мутировать входной document.
- [ ] Запустить `uv run pytest tests/tasks/boards/test_adf.py -q`; ожидать green.
- [ ] Выполнить `uv run ruff check reviewer/tasks/boards/adf.py tests/tasks/boards/test_adf.py`.
- [ ] Закоммитить: `git commit -m "feat: добавить конвертацию Jira ADF (PRI-215)"`.

---

### Task 7: Добавить Jira Cloud sync, single read и normalization

**Files:**

- Create: `reviewer/tasks/boards/jira.py`
- Modify: `reviewer/tasks/boards/registry.py`
- Modify: `reviewer/tasks/boards/attachments.py`
- Create: `tests/tasks/boards/test_jira_read.py`
- Create: `tests/tasks/boards/test_jira_normalize.py`
- Modify: `tests/tasks/boards/test_provider_contract.py`
- Add fixtures: `tests/fixtures/jira/search-page-1.json`
- Add fixtures: `tests/fixtures/jira/search-page-2.json`
- Add fixtures: `tests/fixtures/jira/issue.json`

**Jira constructor and fields:**

```python
class JiraCloudBoard:
    board_type = "jira"
    _FIELDS = (
        "summary,description,status,updated,subtasks,issuelinks,"
        "attachment,issuetype,project"
    )

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        key_pattern: str,
        issue_type: str | None,
        attachment_max_bytes: int,
        attachment_timeout: float,
        attachment_store_chars: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None: ...
```

- [ ] Написать auth/base URL tests: разрешён только `https://<site>` без `/rest/api/3`; Basic auth header декодируется в test transport, но ни header, ни token не попадают в assertion failure/result.
- [ ] Написать enhanced JQL pagination test: `POST /rest/api/3/search/jql`, `jql=project = "PRI" ORDER BY updated ASC`, `fields`, `maxResults`, затем `nextPageToken`; `limit` останавливает yield без лишней страницы.
- [ ] Написать timestamp test на ISO-8601 с `Z` и offset, ожидая epoch milliseconds.
- [ ] Написать mapper parity: `fetch_one("PRI-1")` и та же issue из search дают одинаковый `RawTask`.
- [ ] Запустить `uv run pytest tests/tasks/boards/test_jira_read.py -q`; ожидать import fail.
- [ ] Реализовать Jira client поверх общего `BoardHttpClient`, enhanced search pagination, `_raw_from_issue` и `fetch_one`; 404 возвращает `None`, остальные transport failures становятся `BoardProviderError`.
- [ ] Написать normalization tests: ADF Markdown, status, URL, issue links directions/types, subtasks→criteria, issue/project metadata.
- [ ] Написать attachment tests: same Jira host success, off-host/403/oversize/unsupported дают per-file warning; content caps используют существующий attachment helper.
- [ ] Запустить `uv run pytest tests/tasks/boards/test_jira_normalize.py -q`; ожидать behavior failures.
- [ ] Расширить `RawTask` минимальными provider-neutral metadata при необходимости (`provider_data: dict = field(default_factory=dict)`), не добавляя Jira-specific fields в dataclass.
- [ ] Реализовать `normalize`/`normalize_meta`; unknown ADF warnings и attachment warnings должны доходить в `TaskBrief["warnings"]`, не прерывая задачу.
- [ ] Экспортировать Jira `provider_spec()` с required `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, option `issue_type`; явно зарегистрировать одной строкой в default registry.
- [ ] Добавить Jira fixture в read/normalize части общей contract suite.
- [ ] Запустить:

```bash
uv run pytest tests/tasks/boards/test_jira_read.py \
  tests/tasks/boards/test_jira_normalize.py \
  tests/tasks/boards/test_provider_contract.py -q
```

Ожидание: green, socket blocked.

- [ ] Закоммитить: `git commit -m "feat: добавить чтение задач Jira Cloud (PRI-215)"`.

---

### Task 8: Завершить Jira validation, discovery, create и finish

**Files:**

- Modify: `reviewer/tasks/boards/jira.py`
- Create: `tests/tasks/boards/test_jira_validation.py`
- Create: `tests/tasks/boards/test_jira_targets.py`
- Create: `tests/tasks/boards/test_jira_create.py`
- Create: `tests/tasks/boards/test_jira_finish.py`
- Create: `tests/tasks/boards/test_jira_errors.py`
- Modify: `tests/tasks/boards/test_provider_contract.py`
- Add fixtures: `tests/fixtures/jira/project-statuses.json`
- Add fixtures: `tests/fixtures/jira/transitions.json`

- [ ] Написать validation tests для `/rest/api/3/myself`, project visibility и permissions. Read capability может быть true при create/transition false; safe result:

```python
{
    "status": "ok",
    "identity": {"account_id": "abc", "display_name": "Reviewer Bot"},
    "project": "PRI",
    "capabilities": {"read": True, "create": False, "transition": False},
    "warnings": ["missing Jira permission: CREATE_ISSUES"],
}
```

- [ ] Написать discovery tests: project statuses сгруппированы без duplicates; issue types идут в option `issue_type.choices`; targets имеют stable status id, label и purposes.
- [ ] Запустить validation/targets tests; ожидать отсутствующие methods, затем реализовать `validate_connection` и `list_targets`.
- [ ] Написать create tests: без `project` → configuration error; без `issue_type` → fail-soft config error; ADF create payload; key from response; exact id/label transition; unavailable/ambiguous transition предупреждает и не откатывает issue.
- [ ] Реализовать `create`: один POST issue, затем read transitions и optional POST transition. Не retry write request после отправки.
- [ ] Написать finish tests: existing href не дублируется; new PR link + note; exact transition; already closed; description succeeds/transition denied returns partial flags; write update не откатывается.
- [ ] Реализовать `finish` с независимыми flags `pr_link_added`, `done_set`, `already_closed`, `warnings`; target resolution использует только текущие issue transitions.
- [ ] Написать error tests для 401/403/404/429/5xx: category/retryable корректны; response body/token/email/Auth header/query отсутствуют в exception, MCP response и caplog.
- [ ] Реализовать read-only bounded retry с `Retry-After` и injectable sleeper; 401/403 без retry; create/update/transition не повторять автоматически.
- [ ] Добавить Jira create/discovery/finish/close/error cases в общую contract suite.
- [ ] Запустить:

```bash
uv run pytest tests/tasks/boards/test_jira_*.py \
  tests/tasks/boards/test_provider_contract.py -q
```

Ожидание: green.

- [ ] Прогнать MCP fake/Jira integration tests:

```bash
uv run pytest tests/mcp/test_board_provider_extensibility.py \
  tests/mcp/test_create_task.py tests/mcp/test_finish_task.py \
  tests/mcp/test_get_board_targets.py tests/mcp/test_sync_board.py -q
```

- [ ] Закоммитить: `git commit -m "feat: завершить lifecycle Jira Cloud (PRI-215)"`.

---

### Task 9: Сделать registry-driven credential wizard и `reviewer check`

**Files:**

- Modify: `reviewer/install.py`
- Modify: `reviewer/entrypoints/cli.py`
- Create: `reviewer/tasks/boards/setup.py`
- Modify: `tests/install/test_install_wizard.py`
- Modify: `tests/test_install_wizard.py`
- Create: `tests/install/test_board_setup.py`
- Create: `tests/entrypoints/test_check_boards.py`
- Modify: `.env.example`

**Setup abstraction:**

```python
class SetupIO(Protocol):
    dry_run: bool
    non_interactive: bool
    def confirm(self, text: str, default: bool = False) -> bool: ...
    def prompt(self, text: str, *, secret: bool = False, default: str = "") -> str: ...
    def choose(self, text: str, choices: Sequence[SetupChoice]) -> str: ...
    def open_url(self, url: str) -> None: ...

def board_env_group(registry: BoardProviderRegistry) -> EnvGroup: ...
def configure_board_provider(spec: BoardProviderSpec, io: SetupIO) -> dict[str, str]: ...
```

- [ ] Переписать wizard count tests: board fields и `.env` template формируются из registry metadata; Jira fields присутствуют, secret flags верны, legacy aliases не генерируются.
- [ ] Написать dry-run/yes tests: browser, secret prompt, validation и network hooks не вызываются; preview показывает env keys, но не secret values.
- [ ] Запустить install tests; ожидать fail на статическом `WIZARD_GROUPS`.
- [ ] Реализовать `board_env_group(default_board_registry())`; общие `TASK_BOARD_MCP/KEY_PATTERN/URL_TEMPLATE` оставить отдельными non-secret fields, provider credentials получить из specs.
- [ ] Добавить `reviewer init --dry-run`: команда печатает registry-derived env keys и safe defaults, не открывает browser, не спрашивает secrets, не вызывает validation/network и не пишет файл. `--yes` также не вызывает acquisition/network hooks.
- [ ] Написать Jira setup test: показывает official Atlassian token URL, открывает только после confirmation, скрыто спрашивает email/token, отклоняет `/rest/api/3` base, вызывает validation и сохраняет только три Jira env values.
- [ ] Добавить Jira validation case для token, несовместимого с direct-site auth: safe hint предлагает создать token без scopes; token value и raw 401 body не показываются.
- [ ] Написать YouTrack setup test: URL ведёт на официальную страницу permanent token выбранного instance, есть напоминание про YouTrack service scope и полный `perm:` prefix; validation показывает safe identity/project.
- [ ] Написать YouGile acquisition tests: login/password hidden, companies→choice→API key, password удалён в success/error state; manual key fallback; `allowOnlyOpenId` выдаёт actionable limitation без попытки OAuth exchange.

```python
def test_yougile_password_is_discarded_on_failure(fake_io, caplog):
    fake_io.secret_answers["Password"] = "never-persist"
    with pytest.raises(BoardProviderError):
        acquire_yougile_key(fake_io, client=failing_client())
    rendered = repr(fake_io.saved_values) + caplog.text
    assert "never-persist" not in rendered
    assert "YOUGILE_PASSWORD" not in fake_io.saved_values
```

- [ ] Реализовать provider acquisition hooks. Password хранить в локальной переменной и очищать в `finally`; hooks возвращают только declared credential env keys.
- [ ] Подключить validation в interactive `reviewer init`; при fail дать исправить ввод или продолжить без provider. Не сохранять непроверенные secrets без явного подтверждения.
- [ ] Расширить `reviewer check`: для каждого configured spec вызвать `validate_connection`, вывести type/identity/capabilities/warnings, затем закрыть provider; ошибки sanitize через runtime wrapper.
- [ ] Обновить `.env.example` registry fields и официальные setup URLs/comments; legacy YouGile aliases описать как read-only compatibility, не дублировать secrets.
- [ ] Запустить:

```bash
uv run pytest tests/install/test_install_wizard.py tests/test_install_wizard.py \
  tests/install/test_board_setup.py tests/entrypoints/test_check_boards.py -q
```

Ожидание: green.

- [ ] Закоммитить: `git commit -m "feat: добавить мастер подключения досок (PRI-215)"`.

---

### Task 10: Перевести MCP schemas и skills на generic targets/options

**Files:**

- Modify: `reviewer/entrypoints/mcp_server.py`
- Modify: `plugin/skills/configure-review/SKILL.md`
- Modify: `plugin/skills/sync-tasks/SKILL.md`
- Modify: `plugin/skills/solve-task/SKILL.md`
- Modify: `plugin/skills/create-task/SKILL.md`
- Modify: `plugin/skills/finish-task/SKILL.md`
- Modify: `tests/mcp/test_server_tools.py`
- Modify: `tests/skills/test_configure_review_skill.py`
- Modify: `tests/skills/test_sync_tasks_guardrail.py`
- Modify: `tests/skills/test_solve_task_brief.py`
- Modify: `tests/skills/test_create_task_skill.py`
- Modify: `tests/skills/test_finish_task_skill.py`
- Modify: `tests/skills/test_assembled_prompts.py`

- [ ] Сначала изменить guard tests: новые skills обязаны читать `create_target`, `done_target`, `options`, `targets`, `required_for`, `choices`; не должны содержать closed type list или legacy field names.

```python
@pytest.mark.parametrize("path", BOARD_SKILLS)
def test_board_skills_use_generic_config(path):
    text = path.read_text(encoding="utf-8")
    assert "provider_options" in text or "options" in text
    assert "yougile|youtrack" not in text.lower()
    assert "done_column" not in text
    assert "done_state" not in text
    assert "status_field" not in text
```

- [ ] Запустить skill tests из Files; ожидать failures на старом content.
- [ ] Обновить MCP public docstrings: `board_type` — registered type; `provider_options` — non-secret JSON object; discovery response единый. Удалить provider-specific semantic text.
- [ ] Обновить `configure-review`: вызвать `get_board_targets`, показать normalized targets/options по labels, заполнить generic `.review.yml`, никогда не писать credential values.
- [ ] Обновить `sync-tasks`/`solve-task`: передавать `task_board.options` как `provider_options`; не выставлять default provider-specific option.
- [ ] Обновить `create-task`: использовать `create_target`, `project`, `options`; если required option отсутствует, вызвать discovery/попросить пользователя, а не угадывать.
- [ ] Обновить `finish-task`: явно подтвердить выбранный `done_target` label и передать generic `target/options`; сохранить confirmation-before-write.
- [ ] Добавить assembled prompt assertions, что Jira автоматически поддерживается через generic metadata без отдельного Jira playbook branch.
- [ ] Запустить:

```bash
uv run pytest tests/mcp/test_server_tools.py tests/skills/test_configure_review_skill.py \
  tests/skills/test_sync_tasks_guardrail.py tests/skills/test_solve_task_brief.py \
  tests/skills/test_create_task_skill.py tests/skills/test_finish_task_skill.py \
  tests/skills/test_assembled_prompts.py -q
```

Ожидание: green.

- [ ] Выполнить repository guard:

```bash
rg -n 'yougile\\|youtrack|done_column|done_state|status_field' \
  plugin/skills reviewer/entrypoints/mcp_server.py
```

Ожидание: exit 1. Legacy имена остаются только в server compatibility implementation/tests/docs migration section.

- [ ] Закоммитить: `git commit -m "docs: обобщить навыки работы с досками (PRI-215)"`.

---

### Task 11: Документировать capability matrix, extension path и migration

**Files:**

- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `CLAUDE.md`
- Modify: `.env.example`
- Modify: `.review.yml`
- Create: `docs/board-providers.md`
- Modify: `tests/skills/test_readme_grounding_block.py`
- Create: `tests/docs/test_board_provider_docs.py`

- [ ] Написать docs tests, проверяющие:
  - capability matrix содержит YouGile, YouTrack, Jira и девять lifecycle capabilities;
  - Jira Cloud-only/unscoped token limitation;
  - YouGile OIDC не назван REST OAuth;
  - generic `.review.yml`;
  - compatibility mapping и окно одного релиза;
  - extension checklist требует spec + full contract fixture;
  - новые user-facing sections не содержат `yougile|youtrack` как closed choice.
- [ ] Запустить `uv run pytest tests/docs/test_board_provider_docs.py tests/skills/test_readme_grounding_block.py -q`; ожидать fail до обновления docs.
- [ ] Добавить в `docs/board-providers.md` capability matrix:

| Capability | YouGile | YouTrack | Jira Cloud |
|---|---:|---:|---:|
| Sync/pagination | ✓ | ✓ | ✓ |
| Markdown normalization | HTML↔MD | Native MD | ADF↔MD |
| Links/subtasks | ✓ | ✓ | ✓ |
| Attachments | ✓ | ✓ | ✓ |
| Single read | ✓ | ✓ | ✓ |
| Discovery | ✓ | ✓ | ✓ |
| Create/target | ✓ | ✓ | ✓ |
| Finish/PR link | ✓ | ✓ | ✓ |
| Write-through | ✓ | ✓ | ✓ |

- [ ] Документировать credential setup/rotation для всех трёх providers с официальными URLs, safe storage и `reviewer check`; отдельно указать Jira direct site URL + unscoped token.
- [ ] Документировать generic `.review.yml` и exact legacy mapping. Указать removal не раньше следующего breaking release и ссылку на будущую cleanup task, не удалять compatibility сейчас.
- [ ] Документировать добавление provider: adapter → immutable spec → explicit registry line → full contract fixture → provider-specific tests → docs row; запрет partial registration.
- [ ] Обновить README/README.ru quick-start, tool signatures и config examples; CLAUDE.md invariants привести к registry terminology.
- [ ] Запустить docs tests; ожидать green.
- [ ] Выполнить content guards:

```bash
rg -n 'board_type.*yougile\\|youtrack|type: yougile.*youtrack' \
  README.md README.ru.md CLAUDE.md docs plugin/skills reviewer/entrypoints
```

Ожидание: exit 1 вне явно помеченного legacy migration subsection.

- [ ] Закоммитить: `git commit -m "docs: описать расширяемые провайдеры досок (PRI-215)"`.

---

### Task 12: Полная regression, security и acceptance verification

**Files:**

- Modify only if a failing verification exposes a PRI-215 regression; add the smallest regression test beside the affected module before the fix.

- [ ] Проверить отсутствие незавершённых маркеров в изменённых production/test/docs файлах:

```bash
git diff --name-only -z aec8136..HEAD |
  xargs -0 rg -n \
    -e 'TO''DO' -e 'FIX''ME' -e 'pa''ss$' \
    -e 'NotImplemented''Error' -e 'place''holder' -e 'similar'' to'
```

Ожидание: exit 1. Допустимые исторические совпадения вне diff не учитывать.

- [ ] Проверить generic boundaries:

```bash
rg -n 'yougile|youtrack|jira' \
  reviewer/tasks/sync.py reviewer/tasks/boards/runtime.py reviewer/mcp/service.py
```

Ожидание: concrete type literals отсутствуют; legacy field-name shim в service допустим только с migration test.

- [ ] Проверить credential leakage статически:

```bash
rg -n 'api_token|api_key|password|authorization' \
  reviewer/tasks/boards reviewer/config/provider_credentials.py reviewer/install.py
```

Каждое совпадение вручную классифицировать: input/storage/header construction допустимы; logging, result dict и exception interpolation запрещены.

- [ ] Запустить focused board suite:

```bash
uv run pytest tests/tasks/boards tests/tasks/test_sync.py tests/config \
  tests/mcp/test_sync_board.py tests/mcp/test_create_task.py \
  tests/mcp/test_finish_task.py tests/mcp/test_get_board_targets.py \
  tests/mcp/test_board_provider_extensibility.py tests/install/test_board_setup.py -q
```

Ожидание: green, socket access blocked.

- [ ] Запустить skills/docs suite:

```bash
uv run pytest tests/skills tests/docs tests/install/test_install_wizard.py \
  tests/test_install_wizard.py tests/entrypoints/test_check_boards.py -q
```

Ожидание: green.

- [ ] Запустить полный offline suite: `uv run pytest -q`; ожидать green.
- [ ] Запустить lint: `uv run ruff check .`; ожидать `All checks passed!`.
- [ ] Проверить packaging/imports:

```bash
uv build
uv run python -c \
  'from reviewer.tasks.boards.registry import default_board_registry; print(default_board_registry().registered_types())'
```

Ожидание: wheel/sdist собираются; output содержит `('yougile', 'youtrack', 'jira')` в registry order.

- [ ] Выполнить diff hygiene:

```bash
git diff --check
git status --short
git log --oneline aec8136..HEAD
```

Ожидание: нет whitespace errors; untracked user files не staged; история содержит отдельные task commits.

- [ ] Если verification потребовала исправления, сначала добавить failing regression test, исправить, повторить соответствующий focused suite и полный `pytest`, затем закоммитить `fix: устранить регрессию провайдеров досок (PRI-215)`.
- [ ] Сформировать handoff с exact test/lint/build evidence, Jira first-release limitations и migration window. Не заявлять completion без свежих результатов команд выше.

## Acceptance Traceability

| PRI-215 criterion | Tasks |
|---|---|
| Registry формирует available/configured types | 1–2 |
| Нет concrete branching в factory/MCP/SyncService | 2, 4, 12 |
| Fake provider проходит generic lifecycle | 4 |
| YouGile/YouTrack сохраняют parity | 2–4 |
| Jira full lifecycle + write-through | 6–8 |
| Jira issue type не угадывается | 8 |
| Удобный setup Jira/YouTrack/YouGile | 9 |
| YouGile password только в памяти | 9, 12 |
| Общая contract suite для трёх providers | 3, 7–8 |
| Fail-soft и secret-safe errors | 1, 4, 8, 12 |
| Generic CLI/MCP/config/skills/docs | 5, 10–11 |
| Legacy compatibility + warnings | 4–5, 11 |
| Default suite offline | 3, 7–9, 12 |
