# PRI-213 — Создание задач на доске из reviewer (create_task)

Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-213
Бриф: `docs/superpowers/briefs/2026-07-23-PRI-213-create-task-on-board.md`

## Проблема

Reviewer читает доски по REST (`reviewer/tasks/boards/`, `TaskBoardProvider`) и пишет в них узко:
болк-синк (`sync_board`) и закрытие задачи (`finish_task`). Создать задачу нельзя — её заводят
руками в UI доски или board-MCP на стороне LLM: у каждого клиента свой формат, структура держится
только на дисциплине конкретной модели.

Второй слой проблемы — текст. YouGile хранит `description` как HTML, а `normalize_yougile`
(`reviewer/tasks/boards/yougile.py:63`) кладёт его в стор **как есть**. Поэтому LLM читает через
`get_task` HTML-шум (`<br />`, `&gt;`, `<div>PR: <a href=…>`), а человек в UI видит то, что
сгенерировала модель, не знавшая про транспорт. Проблема воспроизводится на самом акте создания:
задача PRI-213 заведена через board-MCP — YouGile переписал `<br>` в `<br />`, частично съел
HTML-примеры санитайзером и оставил в хвосте непарный `</div>`.

У YouTrack `description` — нативный markdown, и `normalize_youtrack` уже отдаёт чистый текст. То
есть проблема не общая, а транспортная: она принадлежит конкретной доске.

## Решение в одном абзаце

Структура задачи описывается один раз в board-agnostic ядре (`TaskDoc` + `render_markdown`),
общей валютой между ядром и досками становится **канонический markdown**, а конвертация в формат
конкретного транспорта — деталь провайдера. YouGile получает симметричную пару конвертеров
(`md_to_html` при записи, `html_to_md` при чтении), YouTrack — passthrough. Новая доска реализует
`create()` и обязана вернуть markdown из `normalize()` — общий слой при этом не трогается.

## Архитектура

### 1. Ядро: `reviewer/tasks/taskdoc.py` (новый, чистый)

```python
@dataclass
class TaskDoc:
    title: str
    problem: str
    steps: list[str]
    criteria: list[str]
    context: str | None = None

def render_markdown(doc: TaskDoc) -> str: ...
```

`render_markdown` собирает канонический markdown с фиксированным порядком и заголовками:

```
## Проблема

<problem>

## Что сделать

1. <step>
2. <step>

## Критерии приёмки

1. <criterion>

## Контекст

<context>
```

Правила:
- `title` в тело описания **не** входит — это отдельное поле задачи.
- Пустая секция опускается целиком (нет `## Контекст` без текста).
- `steps` и `criteria` — всегда нумерованные списки; элемент рендерится как одна строка.
- Никаких эмодзи, декоративных разделителей и служебных префиксов — ни в шаблоне, ни в валидации.
- Модуль чистый: без I/O, без httpx, без знания о досках. Единственный источник структуры для
  всех досок — нынешних и будущих.

### 2. Контракт провайдера: `reviewer/tasks/boards/base.py`

Добавляется метод в `TaskBoardProvider`:

```python
def create(self, doc_md: str, *, title: str, target: str | None,
           project: str | None) -> dict:
    """Создать задачу из канонического markdown.
    target — доска-специфичная цель (YouGile: title колонки; YouTrack: значение
    status_field). Возвращает {key, url, board_id, target_resolved, warnings}."""
```

Уточняется (кодом и докстрокой) уже существующий контракт:

> `normalize()` и `normalize_meta()` ОБЯЗАНЫ возвращать `description` в markdown.
> Если транспорт доски хранит другой формат — конвертация выполняется внутри провайдера.

Сегодня это неявно верно для YouTrack и неверно для YouGile; после правки — инвариант интерфейса,
обязательный для любой новой доски.

Поведение `create` при недоступной цели — fail-soft: задача создаётся в дефолтном месте доски,
в `warnings` уходит причина, `target_resolved` содержит фактическую цель. Исключение
пробрасывается только если не удалось создать задачу вообще.

### 3. Разметка: `reviewer/tasks/boards/markup.py` (новый, stdlib)

Две функции, поддерживающие узкое подмножество:

`md_to_html(md: str) -> str`
- `##` / `###` → `<h2>` / `<h3>`;
- абзацы (блоки, разделённые пустой строкой) → `<p>`;
- `- ` / `* ` → `<ul><li>`; `1. ` → `<ol><li>`;
- ограждённый блок ``` → `<pre><code>`; инлайн `` ` `` → `<code>`;
- `[текст](url)` → `<a href="url">текст</a>`; голый URL остаётся текстом;
- `**жирный**` → `<strong>`;
- **весь остальной текст экранируется** `html.escape` — включая содержимое инлайн- и блочного кода,
  поэтому `` `<br />` `` в исходнике доезжает до доски как видимый текст, а не как тег.

`html_to_md(html: str) -> str` — на `html.parser.HTMLParser` (`convert_charrefs=True`):
- `h1..h6` → соответствующее число `#` (h1 нормализуется в `##`, чтобы описание не конкурировало
  с заголовком задачи);
- `p`, `div` → блок с пустой строкой после; `br` → перенос строки;
- `ul/ol/li` → `- ` / `1. `;
- `pre`/`code` → ограждённый блок / инлайн-код;
- `a` → `[текст](href)`; если текст совпадает с href — просто URL;
- `strong`/`b` → `**`, `em`/`i` → `*`;
- **неизвестный тег прозрачен**: сам тег отбрасывается, его текстовое содержимое сохраняется;
- HTML-сущности разэкранируются парсером;
- три и более подряд идущих перевода строки схлопываются в два.

Требования к `html_to_md`:
- **Идемпотентность на чистом тексте.** Вход без тегов (а таковы почти все нынешние PRI-задачи —
  markdown, лежащий в HTML-поле как текст) возвращается практически без изменений: заголовки
  `## Проблема` и списки должны выжить.
- **Терпимость.** Функция применяется ко всему, что лежит на доске, включая написанное человеком
  руками. Никогда не бросает: на неразобранном входе возвращает исходную строку.

Известное ограничение (документируется в докстроке): HTML-теги, которые человек написал внутри
инлайн-кода в UI доски, неотличимы от настоящей разметки и будут съедены при чтении. Для текста,
записанного через `create_task`, проблема снята экранированием на стороне `md_to_html`.

### 4. Провайдеры

**YouGile** (`yougile.py`):
- `create`:
  1. Резолв колонки. Из `list_done_targets` выделяется приватный хелпер
     `_columns_of_project(project) -> list[dict]` (обход projects → boards → columns со скоупом по
     префиксу кода и существующим cap 500); `list_done_targets` переписывается на него, поведение
     не меняется. Колонка ищется по точному `title == target`. Не найдена или `target is None` →
     первая колонка первой доски проекта + warning.
  2. `POST /tasks {title, columnId, description: md_to_html(doc_md)}` → uuid.
  3. `GET /tasks/{uuid}` → `idTaskProject` (`PRI-N`): POST возвращает только uuid, ключ проекта
     присваивает доска. Сбой этого GET → fail-soft: `key` = uuid, warning.
  4. `url` = `url_template.replace("{code}", key)`.
- `normalize_yougile`: `"description": html_to_md(raw.description)`. `normalize_meta` идёт через ту
  же чистую функцию, поэтому меняется автоматически.
- `finish` не меняется: он пишет HTML в доску (это правильная сторона), а его идемпотентность
  сверяется с сырым описанием доски, а не со стором.

**YouTrack** (`youtrack.py`):
- `create`:
  1. Резолв проекта: `GET /admin/projects?query=<project>&fields=id,shortName` → id (тот же приём,
     что в `_admin_status_fields:325`). Проект не задан или не найден → `ValueError` (без проекта
     задачу создать нельзя); сервисный слой ловит его и отдаёт клиенту ошибку-словарь — это
     единственный не-fail-soft случай в `create`.
  2. `POST /issues?fields=idReadable {project: {id}, summary: title, description: doc_md}` —
     markdown уходит как есть.
  3. Если `target` задан — структурное обновление кастом-поля `self._status_field` тем же способом,
     что в `finish:255` (JSON, без command-DSL). Ошибка → warning, `target_resolved=None`.
  4. `url` = `{web}/issue/{key}` (та же формула, что в `normalize_youtrack:115`).
- Нормализация не меняется — YouTrack уже отдаёт markdown.

### 5. Сервисный слой и MCP-тул

`MCPReviewService.create_task(title, problem, steps, criteria, context=None, board_type=None,
project=None, target=None) -> dict` — построен по образцу `finish_task` (`service.py:365`):

1. Резолв `board_type`: явный аргумент → иначе единственный из `configured_board_types()` → иначе
   ошибка-словарь `{"status": "error", "reason": …}`.
2. `make_board_provider(settings, board_type)`.
3. `render_markdown(TaskDoc(...))` → `provider.create(...)`.
4. Write-through: `fetch_one(key)` → `normalize` → `task_service.index_task(...)`, best-effort;
   поле `reindexed: bool` в ответе. Задача сразу видна в `get_task`/`search_tasks`, не дожидаясь
   синка (её timestamp выше курсора, но синк может быть не скоро).
5. `provider.close()` в `finally`; любое исключение → ошибка-словарь, не трассировка.

Ответ: `{"status": "ok", "board_type", "key", "url", "target_resolved", "reindexed", "warnings"}`.

Регистрация в `reviewer/entrypoints/mcp_server.py` — сигнатура 1-в-1 с сервисом, докстрока на
английском по образцу `finish_task:120`, с явным указанием, какие аргументы приходят из
`.review.yml` клиента. Креды доски не принимаются и не возвращаются.

### 6. Миграция уже существующих задач

`SyncService._sync_provider` (`sync.py:49`) для задач с `timestamp <= cursor` вызывает
`normalize_meta` → `refresh_meta_batch`, а тот **не трогает description и не эмбедит**
(`service.py:221`). Значит после деплоя старые задачи навсегда остались бы с HTML-описанием в
сторе — критерий «LLM больше не видит HTML» не выполнился бы.

Решение: `sync_board(..., force_renormalize: bool = False)`. При `True` watermark игнорируется —
каждая перечисленная задача идёт через полный `normalize` → `index_batch`. Дедуп по `content_hash`
внутри `index_batch` сам отсеет задачи, чей текст не изменился, поэтому цена прогона — эмбеддинг
только реально изменившихся описаний. Курсор при этом продвигается как обычно. Флаг
пробрасывается через `MCPReviewService.sync_board` и тул `sync_board`; в скилл `sync-tasks`
добавляется строка о том, что это разовая операция после смены нормализации.

### 7. Клиентский скилл `/reviewer_create-task`

`plugin/skills/create-task/SKILL.md` (тело на английском, ответы пользователю на русском — как в
остальных скиллах):

1. **Config.** `task_board` из `.review.yml` репо; нет блока → `get_board_config()`; нет ничего →
   board-less no-op с русским сообщением.
2. **Черновик.** Собрать `problem` / `steps` / `criteria` / `context`, грунтуясь о код через
   `search_codebase` (и при необходимости `callers`/`definition`), чтобы «Проблема» ссылалась на
   `path:line`, а не пересказывала. Прямой запрет на эмодзи и декоративное оформление.
3. **Цель.** `get_board_targets(board_type, project)` → показать доступные колонки/статусы и
   предложить подходящую по теме задачи. Пустой результат → создавать без `target`.
4. **Подтверждение.** Показать заголовок, целевую колонку/статус и полный текст описания; писать
   **только** после явного согласия (та же дисциплина, что в `finish-task`).
5. **Запись.** `create_task(...)`; `status == "error"` → сообщить причину по-русски, fail-open.
6. **Отчёт.** Ключ + ссылка на задачу, перечисленные `warnings`.

Скилл добавляется в `plugin/.claude-plugin/plugin.json` (или эквивалентный перечень скиллов), в
таблицы session-less тулов там, где перечислены остальные, и в оба README.

## Тестирование

Все тесты — unit, без сети (существующий шаблон `tests/tasks/boards/test_yougile_finish.py:19`:
фейковый `_Client` с таблицей маршрутов, провайдер через `__new__` в обход `httpx.Client`).

- `tests/tasks/test_taskdoc.py` — порядок и набор секций; пропуск пустых; нумерация; отсутствие
  заголовка задачи в теле.
- `tests/tasks/boards/test_markup.py` — round-trip `md → html → md` на реальном описании PRI-213;
  идемпотентность `html_to_md` на чистом markdown; `<div>PR: <a href=…>` → `PR: <url>`; неизвестный
  тег отдаёт текст; `<script>` не выживает как тег после `md_to_html`; битый HTML не бросает.
- `tests/tasks/boards/test_yougile_create.py` — резолв колонки по title; дочитывание
  `idTaskProject` вторым GET; описание уходит HTML-ом; ненайденная колонка → первая + warning;
  сбой второго GET → key=uuid + warning.
- `tests/tasks/boards/test_youtrack_create.py` — резолв id проекта; markdown уходит без
  конвертации; установка `status_field` структурным JSON; ошибка установки → warning, задача
  создана.
- `tests/tasks/boards/test_yougile_normalize.py` — дополняется: `description` приходит markdown-ом.
- `tests/tasks/boards/test_base.py` — оба провайдера удовлетворяют расширенному Protocol.
- `tests/mcp/test_create_task.py` — резолв `board_type` (единственный / явный / отсутствующий);
  ошибки словарём; write-through зовёт `index_task`; провайдер закрывается.
- `tests/tasks/test_sync.py` — дополняется: `force_renormalize=True` гонит задачи ниже watermark
  через `normalize`/`index_batch`, а не через `refresh_meta_batch`.
- `tests/skills/test_create_task_skill.py` — guard: скилл существует, упоминает `create_task`,
  `get_board_targets`, требует подтверждения перед записью и ответа по-русски.

## Инварианты, которые нельзя нарушить

1. Креды доски живут только в env сервера и никогда не возвращаются клиенту.
2. Сервер репо-агностичен: `.review.yml` парсит клиент и передаёт значения аргументами.
3. `normalize()` любого провайдера возвращает `description` в markdown.
4. Структура описания задаётся **только** в `taskdoc.py`; провайдер не переписывает секции.
5. Любой пользовательский текст, уходящий в HTML-транспорт, экранируется (защита от stored XSS —
   существующий инвариант `finish`, `tests/tasks/boards/test_yougile_finish.py:75`).
6. Запись в доску происходит только после явного подтверждения пользователя (на стороне скилла).

## Границы скоупа

- Редактирование существующих задач (`update_task`), комментарии, вложения при создании,
  назначение исполнителей — не входят.
- Провайдеры, кроме YouGile и YouTrack, не добавляются; расширяемость обеспечивается контрактом.
- CLI-команда для создания задачи не добавляется (точка входа — MCP-тул и скилл).

## Сопутствующие правки

- README.md и README.ru.md — новый тул `create_task`, скилл `/reviewer_create-task`, флаг
  `force_renormalize`.
- Перечни session-less тулов в скиллах — синхронизировать.
- `update_codex_plugin_manifest.py` — после любых правок под `plugin/`, иначе install-тесты краснеют.
