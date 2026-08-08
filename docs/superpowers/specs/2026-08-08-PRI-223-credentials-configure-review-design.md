# PRI-223 (части C и D) — Provider credentials и configure-review

Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-223

Бриф: `docs/superpowers/briefs/2026-08-07-PRI-223-redesign-reviewer-init.md`

## Скоуп

Эта спека покрывает две оставшиеся части PRI-223:

- C: provider-scoped credential setup для VCS и досок, явные минимальные права,
  read/write-операции, официальный способ получения и проверка подключения;
- D: настройка `repository.primary_branch` / `repository.index_branches` через
  `configure-review`, фиксация модели владения конфигурацией в двух README и focused tests.

Покрываемые пункты «Что сделать»: 1, 5, 9, 10 и оставшаяся часть 13.
Покрываемые критерии: 5, 8, 9, 14 и C/D-часть 15. Общие security/noninteractive
инварианты критериев 7 и 10 сохраняются.

Вне скоупа:

- часть A — per-repo branch resolver, смержена PR #159;
- часть B — global/repo onboarding, смержена PR #160 и уже присутствует в `dev`;
- новый общий runtime registry для VCS и досок;
- проверка точных repository-level write permissions VCS-токена: GitHub/GitLab не дают
  одинакового надёжного API для такой проверки без конкретного PR/MR;
- автоматический запуск индексации или пересборки сводок из `configure-review`.

## Текущее состояние

`reviewer init` уже сохраняет canonical env-поля всех зарегистрированных досок, но в
интерактивном режиме скрывает provider-specific поля до выбора одной доски. Выбранный board
provider конфигурируется через `configure_board_provider`, который показывает `help_text`,
запрашивает только объявленные credentials и вызывает `validate_connection`.

YouGile acquisition уже реализован корректно: hidden login/password, `companies → keys`, выбор
компании, manual fallback, отдельная обработка `allowOnlyOpenId`, очистка password и сохранение
только API key/base URL. Эту логику не нужно переписывать.

VCS остаётся асимметричным: fresh wizard спрашивает `GITHUB_TOKEN`, затем отдельную GitLab-группу
с `GITLAB_TOKEN`, `GITLAB_URL` и `VCS_PROVIDER`. `reviewer check` требует GitHub даже для
GitLab-only deployment и вообще не валидирует GitLab token.

`configure-review` уже умеет выбирать home per-repo или committed policy target и сохранять
чужие policy keys/comments, но блок `repository` не входит в scope скилла. При этом branch config
по архитектуре части A разрешён только в home YAML, а committed `.review.yml` его задавать не
может.

## Рассмотренные подходы

### 1. Расширить существующие контракты — выбран

Оставить board registry и VCS runtime раздельными. Добавить общую маленькую value-модель описания
доступа, заполнить её в существующих board setup specs и в двух VCS setup specs. `init` продолжит
рендерить canonical env целиком, но интерактивно будет prompt-ить только выбранный provider.

Плюсы: минимальный runtime blast radius, registry-driven board path не ветвится, требования к
правам становятся структурными и тестируемыми. Минус: VCS получает небольшой setup catalog,
отдельный от runtime provider selection.

### 2. Единый registry VCS и досок

Вынести factory, credential schema, setup и validation обоих доменов в один provider registry.
Это дало бы формальную симметрию, но потребовало бы переносить стабильный VCS runtime и 11 board
adapters, хотя задача меняет только onboarding metadata. Риск и объём несоразмерны результату.

### 3. Только prompts и документация

Сузить вопросы в `cli.py`, оставив права и операции свободным текстом. Diff был бы меньше, но
полнота metadata не проверялась бы, новые providers легко регрессировали бы, а GitLab-only check
остался бы неверным.

## Архитектура части C

### Общая модель access metadata

Новый модуль `reviewer/config/provider_access.py` содержит только immutable data и renderer:

```python
@dataclass(frozen=True)
class ProviderAccessSpec:
    minimum_permissions: str
    read_operations: tuple[str, ...]
    write_operations: tuple[str, ...]
    validation: str


def render_provider_access(
    *, label: str, help_text: str, help_url: str, access: ProviderAccessSpec
) -> str: ...
```

Renderer выводит в одинаковом порядке:

1. назначение credential;
2. минимальные права;
3. реальные read-операции reviewer;
4. реальные write-операции reviewer;
5. что именно проверяет `reviewer check` / provider validation;
6. официальный URL.

Модуль не знает Click, env, HTTP, VCS или board registry. Значения секретов ему не передаются.

### Board setup metadata

`ProviderSetupSpec` получает обязательное поле `access: ProviderAccessSpec`. Registry validation
требует непустые permissions, read operations, write operations и validation для каждого
зарегистрированного provider. Все 11 production specs заполняют metadata из уже реализованного
lifecycle своих adapters; отсутствие writes представляется явным текстом, а не пустым полем.

`configure_board_provider` вместо свободной пары `help_text + help_url` вызывает общий renderer,
затем сохраняет текущий acquisition/prompt/validation flow без изменений. Это не меняет credential
schema, factory или REST-код adapters.

Для YouGile access metadata явно сообщает:

- официальный hidden-input flow `login/password → companies → API key`;
- admin role не требуется сам по себе: нужен API-capable account с доступом к выбранной компании
  и операциям reviewer;
- при `allowOnlyOpenId` автоматический password flow невозможен, поэтому нужен заранее созданный
  key отдельного API-capable account;
- password не записывается, manual key остаётся fallback.

### VCS setup catalog

`reviewer/install.py` получает immutable `VcsSetupSpec` и catalog из двух записей:

```python
@dataclass(frozen=True)
class VcsSetupSpec:
    provider: Literal["github", "gitlab"]
    label: str
    credential_fields: tuple[EnvField, ...]
    help_url: str
    help_text: str
    access: ProviderAccessSpec
```

GitHub:

- credential: `GITHUB_TOKEN`;
- fine-grained PAT: target repository, Pull requests Read and write, Contents Read;
- reads: PR metadata/diff/comments/content/compare;
- writes: review comments/summary и PR body backlink;
- check: authenticated `/user` identity, без обещания granular repo permission proof.

GitLab:

- credentials: `GITLAB_URL`, `GITLAB_TOKEN`;
- PAT/project token с `api` scope, потому что `read_api` не разрешает публикацию discussions и
  обновление MR description;
- reads: MR metadata/changes/notes/repository files/compare;
- writes: discussions/notes и MR description backlink;
- check: authenticated `/api/v4/user` identity на выбранном base URL.

`VCS_PROVIDER` остаётся runtime fallback. Catalog управляет только onboarding и не заменяет
`repo_vcs`, который по-прежнему заполняется из git remote при индексации.

### Интерактивный `reviewer init`

Canonical `WIZARD_GROUPS` по-прежнему содержит все поддерживаемые env keys. Это нужно для
детерминированного render, redaction, `--yes`/`--dry-run` и сохранения существующих advanced
deployments.

Интерактивный flow меняется так:

1. Prompt обычных групп (`VOYAGE_API_KEY`, stores), исключая provider-specific VCS/board fields.
2. Инициализировать все provider fields текущими значениями или дефолтами, чтобы невыбранные
   существующие credentials не потерялись.
3. Предложить настройку VCS. Default provider: распознанный локальный `origin` через
   `derive_vcs_from_remote`; затем current `VCS_PROVIDER`; затем `github`.
4. Показать structured access metadata выбранного VCS и спросить только его credential fields.
   Записать выбранный тип в `VCS_PROVIDER`.
5. Выполнить уже существующий board selection/setup flow; prompt-ить только выбранную board spec.
6. Показать единый redacted preview и сохранить только после существующего final confirmation.

Невыбранный provider не prompt-ится, не валидируется и не открывает browser. Его existing env
values сохраняются byte-for-value через текущий render plan; на fresh config его canonical fields
остаются пустыми.

`--yes` и `--dry-run` не выбирают provider, не prompt-ят, не открывают browser и не вызывают сеть.
Они используют current/default values ровно как часть B.

### `reviewer check`

Проверка VCS выделяется в `_check_vcs_providers(settings)`:

- `VOYAGE_API_KEY` остаётся обязательным;
- если не настроен ни один VCS token, readiness check завершается ошибкой с подсказкой
  `reviewer init --scope global`;
- каждый реально заданный `GITHUB_TOKEN` / `GITLAB_TOKEN` проверяется независимо;
- GitHub вызывает `https://api.github.com/user`;
- GitLab вызывает `<GITLAB_URL>/api/v4/user` с `PRIVATE-TOKEN`;
- HTTP status и тип исключения выводятся без token/credential URL;
- один невалидный настроенный token делает общий check красным, даже если второй валиден;
- board validation остаётся следующим независимым этапом.

Identity-check подтверждает URL/token/authentication. Exact repository permission проверяется
реальной dry-run/review operation, потому что provider APIs не дают общего достоверного способа
доказать все granular rights заранее. Это ограничение печатается и документируется явно.

## Архитектура части D

### Branch workflow в `configure-review`

Frontmatter и Scope скилла добавляют `repository.primary_branch` и
`repository.index_branches`. Новый branch step выполняется до анализа context policy:

1. Определить git root через `git rev-parse --show-toplevel` и прочитать только `origin` через
   `git remote get-url origin`; network git commands запрещены.
2. Нормализовать `owner/name` теми же правилами SSH/HTTPS, что runtime; при отсутствующем или
   нераспознанном remote спросить repo id.
3. Получить effective primary/index/source через
   `reviewer config show --repo <repo> --json`. Policy/VCS error не отменяет branch section;
   malformed home config остаётся блокирующей безопасной ошибкой.
4. Показать current values/source и спросить новый primary, затем полный ordered unique index list.
   Primary обязан входить в index.
5. Branch destination всегда
   `$XDG_CONFIG_HOME/rag-reviewer/repos/<owner>/<name>.yml`. Committed `.review.yml` не может
   получить `repository`, даже если пользователь выбрал committed target для policy.
6. Прочитать destination, отклонить malformed/non-regular/symlink/credential-like config и
   собрать line-oriented patch. Нельзя сериализовать весь документ через `yaml.safe_dump`.
7. Если `repository` отсутствует, append canonical block. Если присутствует, заменить только
   `primary_branch` и `index_branches`, сохранив sibling/unknown subkeys, top-level keys,
   комментарии, окончания строк и style окружающего документа.
8. Показать path, source, old/new branch values и полный patch до записи; запросить final confirm.
9. После записи повторить `reviewer config show --repo <repo> --json` и проверить exact
   primary/index/source.

Если в одном запуске меняются branches и policy, preview перечисляет оба targets. Branch patch
пишется в home per-repo, policy patch — в ранее выбранный home/committed target. Ни один файл не
меняется до общего final confirmation.

### Rebuild guidance

- новый `index_branches` содержит ещё не индексированную ветку → предложить
  `rag-reviewer:sync-codebase` для этой ветки;
- primary сменился на уже индексированную tracked branch → rebuild не нужен;
- удалённая tracked branch перестаёт выбираться, но старый base index автоматически не удаляется;
- branch changes никогда не запускают `summarize-subsystems`;
- все существующие mapping rules для paths/summary/context/task board сохраняются.

### Документация

README.md и README.ru.md получают синхронные разделы:

1. Таблица «что где хранится»:
   - global `.env`: secrets, DSN, runtime infrastructure и compatibility fallbacks;
   - home global YAML: общие non-secret defaults;
   - home per-repo YAML: branches и operator-owned repo policy;
   - committed `.review.yml`: team-visible policy/task-board metadata без credentials;
   - git remote/CLI: canonical repo identity;
   - Postgres/Neo4j: derived indexes/runtime state, не source of configuration truth.
2. VCS token matrix: получение, минимальные rights, reads, writes, check semantics.
3. Сценарии:
   - single repo: `reviewer init`, review/confirm preview, `reviewer check`;
   - второй repo: `reviewer init --scope repo`, без изменения global `.env`;
   - CI/server: secrets из secret manager/env, noninteractive init только для deterministic
     preview/write, home config принадлежит service account, team policy хранится committed.
4. Удаление устаревшего GitLab-only workaround после появления GitLab check.
5. Skill reference переименовывается из «update `.review.yml`» в layered policy + branches.

`docs/board-providers.md` остаётся authoritative board reference и получает только уточнения,
которые соответствуют structured access metadata; VCS matrix живёт в README, а не в board docs.

## Ошибки и безопасность

- Любой setup exception до final preview не пишет `.env` или home YAML.
- Secret values не входят в access metadata, preview, error, docs или validation report.
- Existing credentials невыбранного provider не удаляются и не эхоятся.
- Ошибка одного configured VCS в `check` не скрывается валидностью другого.
- `configure-review` fail-closed для ambiguous/malformed/credential-like home YAML и не
  откатывается молча на lower layer.
- Committed `.review.yml` никогда не получает branch config.
- YouGile login/password остаются transient local variables; automatic acquisition не запускается
  в noninteractive modes.

## Тестирование

Все тесты unit, без реальной сети и localhost.

### Provider metadata и setup

- `tests/tasks/boards/test_registry.py`: каждый registered spec имеет полный access contract;
- provider-specific contract tests: permissions/operations соответствуют adapter capabilities;
- `tests/tasks/boards/test_setup.py`: renderer output, no secret values, acquisition/validation
  порядок;
- YouGile tests: companies/key success, manual fallback, `allowOnlyOpenId`, no admin claim,
  password redaction/cleanup, noninteractive no-op.

### Init и check

- выбран GitHub → prompt только `GITHUB_TOKEN`, GitLab fields не prompt-ятся;
- выбран GitLab → prompt только `GITLAB_URL`/`GITLAB_TOKEN`, GitHub не prompt-ится;
- board provider contract остаётся scoped;
- existing credentials невыбранного provider сохраняются;
- fresh unselected credentials остаются пустыми;
- access metadata показана до secret prompt;
- `--yes`/`--dry-run` не prompt-ят, не валидируют и не используют сеть;
- GitHub-only, GitLab-only и dual-token `reviewer check`;
- no-token failure, per-provider HTTP failure и redaction.

### Configure-review и docs

- guard требует repo autodetect, `config show`, primary/index questions и source display;
- guard запрещает запись `repository` в committed `.review.yml` и whole-file YAML rewrite;
- guard требует сохранения sibling keys/comments, preview/confirm и post-write verification;
- guard фиксирует branch rebuild guidance без запуска skills;
- README tests требуют одинаковый порядок paired sections, storage table, три scenarios,
  VCS matrix и отсутствие GitLab-only workaround.

### Команды проверки

```bash
.venv/bin/pytest tests/install tests/entrypoints tests/tasks/boards tests/skills tests/docs -q
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```

## Acceptance mapping

| Критерий | Реализация |
|---|---|
| 5. configure-review пишет branches, сохраняя ключи/comments | Branch workflow и guard tests части D |
| 7. Secrets только env | Existing credential rejection + preview/check redaction |
| 8. Только credentials выбранного provider | Scoped VCS и существующий scoped board flow |
| 9. YouGile key flow без admin requirement | Existing acquisition + structured access text/tests |
| 10. `--yes`/`--dry-run` без prompt/network/leak | Existing noninteractive path + new setup guards |
| 14. README ownership/scenarios | Paired storage table, VCS matrix и три deployment scenarios |
| 15. Tests C/D | Provider, CLI/check, skill и docs suites |

## Риски

- Обязательные access fields затрагивают 11 board specs. Это намеренный compile/test-time guard:
  новый provider нельзя зарегистрировать без setup/security описания.
- `WIZARD_GROUPS` одновременно служит render schema и prompt catalog. Интерактивная фильтрация
  должна жить в маленьких helper-функциях; нельзя создавать второй список env keys вручную.
- Exact permission wording быстро устаревает у SaaS providers. Официальный URL остаётся source of
  truth, а tests проверяют полноту полей и ключевые минимальные scopes, не копируют всю внешнюю
  документацию.
- Prompt skill не является Python runtime. Надёжность branch edit обеспечивается exact
  line-oriented instructions, preview/confirm, fail-closed правилами и post-write `config show`;
  новый provider-specific YAML writer в рамках этой задачи не вводится.
