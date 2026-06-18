# PRI-122: reviewer init — интерактивный мастер настройки .env

**Дата:** 2026-06-19
**Статус:** согласован

## Проблема

`reviewer init` сейчас просто копирует минимальный `ENV_TEMPLATE` в `~/.config/rag-reviewer/.env`.
При существующем файле отказывает (или перезаписывает с `--force`). Никакой интерактивности:
новый пользователь должен вручную открыть файл, разобраться что заполнить и в каком формате.

## Цель

Превратить `reviewer init` в пошаговый wizard, который:
- читает текущий `.env` и использует значения как дефолты
- запрашивает VOYAGE_API_KEY и GITHUB_TOKEN с маскировкой ввода
- предлагает опциональные группы настроек (БД, multi-repo, task board)
- работает в non-interactive режиме (`--yes`) для CI/скриптов
- в конце предлагает запустить `reviewer check`

## Архитектура

### Затрагиваемые файлы

**`reviewer/install.py`** — новые структуры и функции:

```python
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
    fields: list[EnvField]
    optional: bool = False   # опциональные группы предваряются вопросом "настроить?"
```

- `WIZARD_GROUPS: list[EnvGroup]` — список всех групп (см. раздел «Группы и поля»)
- `read_env(path: Path) -> dict[str, str]` — парсит `KEY=VALUE`, пропускает комментарии
- `prompt_groups(groups, current, yes) -> dict[str, str]` — итерирует группы/поля, возвращает итоговый словарь
- `render_env(values: dict, extra: dict) -> str` — генерирует `.env` с разделителями-комментариями; `extra` — поля вне групп wizard (сохраняются в блоке `# Прочие настройки`)

**`reviewer/entrypoints/cli.py`** — команда `init`:

```
reviewer init [--path PATH] [--yes]
```

- `--force` убирается: init всегда работает с существующим файлом (читает и обновляет)
- `--yes` — non-interactive, принимает все дефолты
- делегирует всю логику в `install.py`, сам остаётся ~20 строк

## Группы и поля wizard

### Обязательные (всегда запрашиваются)
| Ключ | secret | required |
|---|---|---|
| `VOYAGE_API_KEY` | да | да |
| `GITHUB_TOKEN` | да | нет |

### Опциональные группы (предваряются вопросом «настроить? [y/N]»)

**Хранилища (Postgres / Neo4j):**
`PG_DSN`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`

**Мульти-репо / ветки:**
`DEFAULT_REPO`, `REVIEW_BRANCHES`

**Доска задач:**
`TASK_BOARD_TYPE`, `TASK_BOARD_MCP`, `TASK_BOARD_KEY_PATTERN`, `TASK_BOARD_URL_TEMPLATE`

Поля тюнинга ревью (`REVIEW_SEVERITY_THRESHOLD`, `REVIEW_MAX_COMMENTS` и др.) в wizard **не включаются** — меняются редко, достаточно `.env.example` как образца.

## UX-поток

### Интерактивный режим

```
Настройка rag-reviewer: ~/.config/rag-reviewer/.env
─────────────────────────────────────────────────
[Обязательные]
VOYAGE_API_KEY: (уже задан — Enter чтобы оставить): _
GITHUB_TOKEN: (уже задан — Enter чтобы оставить): _

Настроить хранилища (Postgres / Neo4j)? [y/N]: _
  PG_DSN [postgresql://reviewer:reviewer@localhost:5433/reviewer]: _
  ...

Настроить мульти-репо / ветки? [y/N]: _
Настроить доску задач? [y/N]: _

✓ Записан ~/.config/rag-reviewer/.env

Запустить reviewer check сейчас? [Y/n]: _
```

**Секреты с существующим значением:** показывать `(уже задан — Enter чтобы оставить)`, не раскрывать значение. Пустой ввод = оставить текущее.

### Non-interactive режим (`--yes`)
- Опциональные группы пропускаются
- Для VOYAGE_API_KEY / GITHUB_TOKEN берётся текущее значение из `.env` (если есть), иначе остаётся пустым
- Файл создаётся/обновляется без prompt'ов — для CI и скриптов

### Сохранение незатронутых полей
`read_env` читает весь файл. Поля вне групп wizard (тюнинг ревью и т.д.) сохраняются через `render_env(..., extra=...)` в блоке `# Прочие настройки` в конце файла.

## Обработка ошибок

| Ситуация | Поведение |
|---|---|
| Файл не читается | `click.ClickException` с понятным сообщением |
| Файл не записывается | `click.ClickException`; директория создаётся через `mkdir(parents=True)` |
| `Ctrl+C` в wizard | `KeyboardInterrupt` ловится, файл **не перезаписывается** (пишем только после полного прохода) |
| `--yes` + нет `.env` | Создаём файл с дефолтами и пустыми секретами, без ошибки |

## Тесты

Файл: `tests/test_install_wizard.py` (unit, без внешних зависимостей)

- `read_env`: парсит `KEY=VALUE`, пропускает `#`-комментарии и пустые строки
- `prompt_groups` с `yes=True`: возвращает текущие значения без вызова `click.prompt` (мокаем prompt)
- `render_env`: поля wizard на месте, «прочие» поля из исходного `.env` сохранены в блоке extra
- `reviewer init --yes` через `click.testing.CliRunner`: создаёт файл, не задаёт вопросов, не падает
