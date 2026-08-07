# PRI-227 — Короткие имена skills

Источник: `docs/superpowers/briefs/2026-07-31-PRI-227-remove-reviewer-skill-name-duplication.md`.

## Цель

Убрать повтор `reviewer` из публичных имён skills, сохранив namespace плагина
`rag-reviewer`. После миграции имя каждого skill определяется basename его каталога:

```text
plugin/skills/<skill-name>/SKILL.md
                    ↕
frontmatter name: <skill-name>
```

Claude Code должен показывать `/rag-reviewer:<skill-name>`, а Codex —
`$rag-reviewer:<skill-name>`. Старые имена вида
`rag-reviewer:reviewer_<skill-name>` перестают поддерживаться.

## Решения

### Атомарное breaking-переименование

Все зарегистрированные `plugin/skills/*/SKILL.md` переводятся на новый контракт одним
изменением. Каталоги skills, namespace плагина и структура общего Claude/Codex payload
не меняются. Compatibility wrappers, aliases и host-specific преобразование payload не
добавляются: документированных безопасных aliases нет, а отдельные wrapper-skills
создали бы вторые команды и нарушили критерий отсутствия старых имён.

Переименование охватывает все существующие skills. Будущий `decompose-task` не входит в
эту задачу как новая функция, но при добавлении обязан сразу иметь
`name: decompose-task`, потому что общий guard применяется к любому новому каталогу с
`SKILL.md`.

### Канонический inventory

Единственный inventory — каталоги первого уровня под `plugin/skills/`, содержащие
`SKILL.md`. Служебный `_common` не регистрируется, поскольку собственного `SKILL.md` у
него нет. Installer и payload по-прежнему используют динамическое обнаружение
каталогов; отдельный реестр публичных имён не вводится.

Frontmatter каждого найденного файла обязан удовлетворять трём инвариантам:

1. `name` в точности равен basename каталога;
2. имена уникальны;
3. имя не начинается с `reviewer_`.

Первый инвариант делает каталог и frontmatter одним контрактом, второй даёт понятную
диагностику при ошибочном inventory, третий явно защищает цель PRI-227.

### Поверхности миграции

Обновляются только актуальные потребители имён:

- frontmatter всех зарегистрированных skills;
- cross-skill ссылки внутри `plugin/skills/`;
- `README.md`, `README.ru.md`, `CLAUDE.md`, `AGENTS.md` и `plugin/README.md`;
- актуальные комментарии и fixtures в `tests/`;
- hook attribution и его тесты, если значение не соответствует новому каноническому
  имени;
- Claude/Codex payload, manifest и installer expectations.

`plugin/hooks/brief_guard.py` уже использует каноническое
`rag-reviewer:solve-task`; его значение сохраняется и покрывается тестом. Миграция не
должна механически менять уже корректные строки.

Исторические артефакты в `docs/superpowers/briefs/`,
`docs/superpowers/specs/` и `docs/superpowers/plans/` не переписываются: они фиксируют
состояние и решения прошлых задач и не являются активной пользовательской
документацией.

## Поток данных

1. Разработчик добавляет или изменяет каталог `plugin/skills/<name>/`.
2. Structure guard читает все `plugin/skills/*/SKILL.md` и проверяет каноническое имя.
3. Installer собирает общий payload из тех же каталогов без переименования.
4. Claude Code добавляет namespace плагина и показывает
   `/rag-reviewer:<name>`.
5. Codex устанавливает тот же payload и показывает `$rag-reviewer:<name>`.

Ни один слой после frontmatter не переводит имя и не содержит параллельной таблицы
aliases. Благодаря этому Claude и Codex не могут разойтись из-за двух независимых
mapping-слоёв.

## Guard и диагностика

Добавляется один data-driven тест, который:

- динамически находит все зарегистрированные skill-файлы;
- разбирает только YAML frontmatter;
- сравнивает `name` с именем каталога;
- проверяет уникальность и запрещённый префикс;
- при ошибке сообщает путь, ожидаемое и фактическое имя.

Точечные assertions вроде `name: reviewer_configure-review` и
`name: reviewer_finish-task` удаляются или заменяются проверками поведения конкретного
skill. Naming contract больше не дублируется в per-skill тестах.

Отдельная проверка активных поверхностей запрещает старые invocation-строки в
production-коде, plugin skills, актуальной документации, hooks и tests. Исторические
superpowers-артефакты задаются явным исключением, чтобы тест не требовал переписывать
историю. Проверка должна ловить именно публичные имена и cross-skill references, а не
произвольные внутренние Python-идентификаторы с допустимым словом `reviewer`.

## Packaging и установка

Существующее динамическое обнаружение каталогов в installer сохраняется. Тесты должны
доказать, что:

- Claude и Codex получают один и тот же набор каталогов skills;
- `_common` не попадает в зарегистрированный набор;
- snapshot/hash меняется после правок payload и проходит штатную проверку cachebuster;
- Codex manifest продолжает ссылаться на `./skills/`;
- локальная загрузка Claude через `--plugin-dir ./plugin` видит короткие имена;
- dry-run/fixture-сценарии обновления не сохраняют старые invocation names.

Реальные внешние API в тестах не вызываются. CLI и файловая система используются через
существующие fakes/fixtures.

## Breaking migration

Актуальная документация содержит таблицу или компактный список перехода со старого
формата на новый и требует:

1. обновить глобальный плагин штатной командой installer;
2. удалить или обновить устаревший cache штатным upgrade-потоком;
3. открыть New Chat или новую CLI-сессию;
4. в IDE выполнить Reload Window;
5. использовать только новые invocation names.

Legacy alias не обещается. Вызов старого имени должен отсутствовать в селекторе, а не
перенаправляться скрыто.

## Проверка

Минимальная проверка изменения:

1. data-driven structure guard для всех skills;
2. существующие `tests/skills/` после обновления cross-references;
3. hook tests для `rag-reviewer:solve-task`;
4. Claude/Codex payload и installer tests;
5. repo-wide проверка отсутствия старых invocation-строк на активных поверхностях;
6. локальная проверка `claude --plugin-dir ./plugin`;
7. Codex plugin payload/manifest verification и installer dry-run;
8. полный релевантный pytest-набор и `git diff --check`.

Если локальный Claude/Codex CLI недоступен, автоматические тесты остаются обязательными,
а пропущенная smoke-проверка явно фиксируется в отчёте вместо ложного успеха.

## Вне scope

- aliases, wrappers и поддержка старых invocation names;
- host-specific payload для Claude или Codex;
- создание `decompose-task`;
- изменение namespace `rag-reviewer`;
- переименование каталогов skills;
- переписывание исторических superpowers-артефактов;
- несвязанный рефакторинг installer или hooks.

## Критерии готовности

- каждый зарегистрированный skill имеет `name`, равный basename каталога;
- Claude и Codex используют короткие namespaced invocation names;
- старые invocation-строки отсутствуют на всех активных поверхностях;
- новый guard отклоняет mismatch, duplicate и префикс `reviewer_`;
- общий payload и оба install-пути проходят проверки;
- breaking migration и требования к новой сессии/Reload Window документированы;
- исторические артефакты сохранены без массового редактирования.
