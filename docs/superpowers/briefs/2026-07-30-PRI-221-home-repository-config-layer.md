# Brief — PRI-221 Домашний слой конфига репозиториев в каталоге reviewer

https://ru.yougile.com/team/686c049c8af8/#PRI-221

## Task

- Данные задачи получены из reviewer store после инкрементного sync board; нормализованный ключ `ID-274`, алиас `PRI-221`.
- Ввести `$XDG_CONFIG_HOME/rag-reviewer/review.yml` (дефолты) и `repos/<owner>/<name>.yml` (per-repo) без чтения рабочего дерева репозитория.
- Централизовать merge верхними ключами: env → home global → committed `.review.yml` целевой ветки → home per-repo; разделить `ReviewPolicy.load_data` и совместимую `load`.
- Перевести шесть существующих чтений, включая index; добавить `config show`, `config migrate`, источники и shadowing, защиту от credential-ключей и audit в `review_runs`.
- Обновить configure-review и оба README; критерии включают обратную совместимость, замену (не склейку) `paths.ignore`, идемпотентную миграцию и отсутствие секретов в выводе.

## Related work

- PRI-133 — сохранить контракт конфигурации VCS: credential-подобные значения не должны утечь из YAML-слоёв, ENV остаётся безопасным fallback.
- PRI-168 — переиспользовать модель `.review.yml` для `paths.ignore` и обновить configure-review так, чтобы домашний per-repo слой был рекомендуемым местом записи.
- PRI-170 — `task_board` уже является repo-scoped политикой; домашний слой должен участвовать в её эффективном разрешении.
- PRI-202 — `context_limits` уже конфигурируемы политикой; новый resolver обязан доставлять их во все потребляющие MCP/CLI пути.
- PRI-220 — README.md и README.ru.md уже проходят переработку; добавить проверяемое описание слоёв, приоритета, рисков сервисного аккаунта и migration path.

(dropped 25: текущая задача, а также найденные задачи о несвязанных возможностях reviewer не задают реализацию слоёв конфигурации.)

## Subsystems

- reviewer/policy — центральная модель `ReviewPolicy`, YAML-over-ENV семантика, `paths.ignore`, `task_board` и `context_limits`.
- reviewer/config — `Settings` и существующий XDG-каталог для `.env`; точка для разрешения домашнего config directory.
- reviewer/services — подготовка ревью берёт policy из целевой ветки и должна сохранить этот PR-safety инвариант.

## Relevant code

(dropped 0: reviewer `search_codebase` на `main` не вернул line-numbered snippets; требуемые task locations нельзя выдавать за retrieval-grounding.)

## Test exemplars

(dropped 0: targeted `include_tests=True` search на `main` не вернул test snippets.)

## Constraints / open questions

- Конфигурация репозитория должна по-прежнему читаться из целевой ветки через VCS API; незакоммиченный `.review.yml` в рабочем дереве вне скоупа.
- Per-repo home layer выше committed YAML снижает видимость политики команде: показать источники/shadowing, предупреждать migrate и сохранять источники в `review_runs`.
- `criteria=[]` в store, но описание содержит раздел «Критерии приёмки», поэтому отдельного thin-criteria gap нет.
- Code/test retrieval index `main` вернул пустую выдачу; перед проектированием потребуются точные code references из свежего индексного поиска.

Собран на: mid/gpt-5.6-terra, режим: subagent
