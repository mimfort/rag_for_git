# PRI-226 — инкрементальные subsystem summaries на уровне файлов

> Бриф: `docs/superpowers/briefs/2026-07-31-PRI-226-incremental-file-summary-fragments.md`

## Цель

Уменьшить LLM-стоимость обновления subsystem summaries: при изменении одного файла
пересуммаризировать только этот файл, переиспользовать fragments остальных файлов и
пересобрать совместимую итоговую cluster-summary. Свежесть по-прежнему определяется
текущим `skeleton_hash`, а гонка между чтением плана и записью не должна делать
неконсистентный кластер fresh.

## Не входит в скоуп

- Переход с `skeleton_hash` на full-content fingerprint.
- Изменение публичного read-контракта `get_subsystem_summaries` или ANN-поиска.
- Запуск LLM внутри reviewer-сервера.
- Изменение текущей path-based кластеризации и правил `summary_cluster_depth`.

## Рассмотренные подходы

### 1. Read-only work payload + атомарный batch persist — выбран

Сервер строит file delta и отдаёт сохранённые fragments. Skill создаёт по одному
file-summary job только для added/changed файлов, затем собирает cluster-summary из
старых и новых fragments. Сервер повторно проверяет aggregate hash и одной транзакцией
сохраняет новые fragments, переносы, удаления и итоговую cluster-summary.

Плюсы: LLM остаётся на клиентской стороне; нет частично записанного состояния; одна
optimistic-граница защищает весь bundle; старый cluster-level read API не меняется.
Минус: write payload становится богаче.

### 2. Отдельный persist каждого fragment

Каждый file-summary сразу записывается отдельным MCP-вызовом, затем отдельным вызовом
пишется cluster-summary. Это проще локально, но прерванный прогон оставляет частичное
состояние, а удаление, перенос и rollback при гонке требуют дополнительного протокола.

### 3. Server-side LLM pipeline

Сервер сам читает файлы и генерирует fragments. Это скрывает orchestration от skill, но
ломает текущую архитектуру: reviewer-server не владеет LLM-сессией и модельным выбором,
а глобальный плагин теряет переносимость между Codex, Claude Code и другими клиентами.

## Модель данных

### `subsystem_summary_fragments`

Новая таблица:

```text
repo         text
branch       text
cluster_key  text
path         text
fingerprint  text
summary      text
provenance   jsonb
updated_at   timestamptz
PRIMARY KEY (repo, branch, cluster_key, path)
```

`provenance` хранит безопасные несекретные поля генерации:
`generator`, `model_tier`, `mode`. Сервер не требует конкретного имени модели и не
переносит credentials в store.

### `subsystem_summary_state`

Новая таблица с одной строкой на `(repo, branch)` хранит последний полностью
завершённый `depth`. Она нужна, чтобы отличать обычный перенос файла между кластерами
от смены depth, которая обязана вызвать полный rebuild.

Состояние depth обновляется только после полного uncapped прохода в
`prune_subsystem_summaries`. Если проход прерван, следующий запуск снова считается
full rebuild. Это дороже повторного продолжения, но не позволяет частично перестроенной
раскладке ошибочно стать incremental.

Существующая таблица `subsystem_summaries` и её primary key не меняются.

## Fingerprint и delta

Для каждого path сервер группирует текущие base-members и вычисляет fingerprint той же
функцией, что aggregate freshness:

```text
file_fingerprint = sha256(sorted(node_id + skeleton_hash для symbols файла))
```

Aggregate `source_hash` кластера остаётся текущим hash от всех
`(node_id, skeleton_hash)`. Поэтому существующие summaries, stale-аннотация и consumers
сохраняют семантику.

Классификация файла:

- `reused`: fragment с тем же path, cluster_key и fingerprint;
- `moved`: fragment с тем же path и fingerprint найден в другом кластере при неизменном depth;
- `changed`: fragment для path существует, но fingerprint отличается;
- `added`: fragment для path отсутствует;
- `removed`: сохранённый fragment текущего кластера больше не принадлежит кластеру.

При bootstrap без fragment state все файлы считаются `added`. При смене depth все
текущие файлы считаются `changed`, даже если fingerprint совпадает: cross-cluster reuse
запрещён до полного завершения rebuild.

## MCP-контракты

### `list_subsystem_clusters`

Существующие поля сохраняются. Для каждого возвращённого кластера добавляются:

```json
{
  "added_files": [{"path": "...", "fingerprint": "..."}],
  "changed_files": [{"path": "...", "fingerprint": "..."}],
  "removed_files": ["..."],
  "moved_files": [{"path": "...", "from_cluster_key": "...", "fingerprint": "..."}],
  "reused_files": 3,
  "bootstrap": false,
  "full_rebuild": false
}
```

На верхнем уровне добавляется `deferred_files`: число added/changed file jobs в
кластерах, отложенных существующим cluster cap. Полные fragment texts сюда не входят,
чтобы listing не раздувал контекст.

### `get_subsystem_summary_work`

Новый read-only session-less tool:

```text
get_subsystem_summary_work(repo, branch, cluster_key, source_hash)
```

Он повторно выводит current members и возвращает:

- `ready=false` и note, если переданный aggregate hash уже устарел;
- added/changed/removed/moved delta;
- `reused_fragments` с `path`, `fingerprint`, `summary`, `provenance`;
- `bootstrap` и `full_rebuild`.

Skill вызывает tool только для stale-кластеров, прошедших cap.

### `index_subsystem_summary`

Текущие обязательные аргументы сохраняются. Добавляется опциональный
`fragments` — список новых file summaries:

```json
{
  "path": "reviewer/index/store.py",
  "fingerprint": "...",
  "summary": "...",
  "provenance": {
    "generator": "rag-reviewer:summarize-subsystems",
    "model_tier": "cheap",
    "mode": "subagent"
  }
}
```

Перед записью сервис заново строит current path map и aggregate hash.

- Hash не совпал: `stored=false`, `race=true`; fragments, summary и embedding не меняются.
- Payload не покрывает ровно все текущие added/changed файлы или содержит неверный
  fingerprint/path: `stored=false` с безопасной note; ничего не меняется.
- Проверка прошла: `SummaryStore` одной транзакцией upsert-ит новые fragments,
  переносит matching moved fragments, удаляет removed/orphaned rows текущего bundle и
  сохраняет cluster-summary с `embedding=NULL`.

Результат содержит `created`, `reused`, `removed`, `moved`, `members` и `stored`.

Для совместимости старый вызов без `fragments` остаётся допустимым: он использует
строгую optimistic-проверку и сохраняет только cluster-summary. Следующий новый skill
увидит отсутствие fragments и выполнит bootstrap.

## Embedding

Embedding вычисляется только после успешного commit итогового текста. После commit
сервис вызывает Voyage и записывает вектор через compare-and-set
`set_embedding_if_source_hash`. Если source hash уже изменился, вектор не записывается.
Если Voyage недоступен, summary остаётся доступной с `embedding=NULL`, а существующий
`backfill_summary_embeddings` дозаполнит её позже.

Старый вектор не сохраняется для нового `source_hash`: иначе ANN временно искал бы новый
текст по embedding старой summary.

## Skill pipeline

Обновлённый `summarize-subsystems`:

1. Вызывает `list_subsystem_clusters` и сохраняет текущие confirmation/depth/cap gates.
2. Для каждого stale-кластера вызывает `get_subsystem_summary_work`.
3. Создаёт ровно один file-summary subagent job на каждый path из
   `added_files + changed_files`. Job читает только свой файл и возвращает один
   grounded fragment; unchanged paths не передаются в Read/subagent prompt.
4. Собирает ordered набор из `reused_fragments`, moved fragments и новых fragments.
5. Отдельный cluster composer получает только fragment texts, а не исходные файлы, и
   возвращает совместимые `title`/`summary`.
6. Вызывает расширенный `index_subsystem_summary`. Race считается deferred work, а не
   успехом.
7. На полном проходе вызывает `prune_subsystem_summaries`; тот удаляет orphaned
   summaries/fragments и фиксирует завершённый depth.
8. Запускает embedding backfill и сообщает метрики:
   created, reused, removed, moved, deferred/raced и embedded.

## Bootstrap и depth rebuild

После обновления существующие cluster summaries продолжают читаться без изменений.
Отсутствие fragments/state делает первый write-проход bootstrap: skill создаёт
fragments всех файлов, но старая cluster-summary не удаляется до успешного атомарного
persist новой.

Смена depth определяется сравнением effective depth с `subsystem_summary_state`. Все
файлы новых кластеров пересуммаризируются; fragments старой раскладки не
переиспользуются. Только полный uncapped prune удаляет старые cluster summaries и
fragments и переводит state на новый depth.

## Ошибки и конкурентность

- Пустой base и недоступные store/graph сохраняют текущий fail-open read behavior.
- Невалидный fragment payload не вызывает частичный commit.
- Изменение base между list/work/index отклоняет весь bundle.
- Повтор идентичного вызова идемпотентен: fingerprint и primary keys не плодят строки.
- Прерванный bootstrap/depth rebuild не удаляет доступные старые summaries.
- Prune не выполняется при `deferred > 0`, как и сейчас.

## Тестирование

### Unit

- Детерминированный per-file fingerprint на основе `skeleton_hash`.
- Delta для unchanged, changed, added, removed и moved paths.
- Full rebuild запрещает reuse при смене depth.
- Work tool возвращает только fragments, нужные composer, и отклоняет stale hash.
- Index отклоняет race/неполный payload без вызова store/embedder.
- Успешный index передаёт atomically complete bundle и только затем вызывает embedder.
- Compare-and-set не пишет embedding для уже сменившегося source hash.
- Старый `get_subsystem_summaries` shape и stale semantics не меняются.
- Skill prompt создаёт jobs только для added/changed paths и отчёт содержит все метрики.

### Integration

- Schema round-trip fragments/provenance/timestamps.
- Atomic create/reuse/remove/move bundle.
- Ошибка внутри транзакции не оставляет частичных fragments.
- Prune удаляет orphaned summaries/fragments и обновляет depth state.
- Bootstrap сохраняет старую cluster-summary до успешного нового commit.

### Полная проверка

- Точечные unit/integration suites для summaries.
- Все unit tests: `pytest -q`.
- Ruff: `ruff check .`.
- Installer/plugin guard tests и Codex install dry-run для глобального плагина.
