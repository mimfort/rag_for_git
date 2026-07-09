# Design — PRI-209: Улучшения плагина после анализа PRI-208 и трейса в БД

## Оценка реальности задачи

Задача **реальна и воспроизводима**. В `reviewer/mcp/service.py:_record_history` метаданные прогона захардкожены (`model="claude-code"`, `duration_ms=0`, `usage=None`, `total_cost=None`, `steps=None`), а в БД действительно отсутствуют шаги трейса. PRI-208 ввёл sidechain-сборку брифа, который `plugin/hooks/brief_cost.py` не видит. Step 1.5 `solve-task` не учитывает auto permission mode. Все четыре дефекта имеют конкретные точки в коде и покрываются тестами.

## Цели и не-цели

**Цели:**
1. Передавать в `ReviewHistory.record_run` реальные `started_at`, `finished_at`, `duration_ms`, `model`, `usage`, `total_cost`.
2. Реализовать серверный `review_steps` — фиксацию MCP-вызовов/этапов прогона.
3. Научить `brief_cost` учитывать sidechain-токены сабагента (PRI-208) или явно документировать ограничение.
4. Сделать Step 1.5 `solve-task` fail-open в auto permission mode — выбирать mid tier без вопроса.
5. Добавить guard/alert на отсутствие свежих `review_runs` для репозитория.

**Не-цели:**
- Менять схему БД `review_runs`/`review_findings`/`review_steps` — она уже поддерживает нужные поля.
- Реализовывать why-trace отдельной находки (PRI-131) — это смежная задача.
- Менять ценообразование/тарификацию — только запись доступных метрик.

## Варианты решений и выбор

### 1. Передача метаданных прогона

- **A. Расширить `publish_review`:** добавить опциональные параметры `model`, `usage`, `total_cost`, `started_at`, `steps`. Клиентский skill передаёт их вместе со summary.
  - *Плюсы:* одна точка интеграции, минимальные изменения в MCP-сервере.
  - *Минусы:* skill должен сам собирать usage/cost по всем сабагентам.
- **B. Отдельный MCP-tool `submit_review_metadata`:** клиент вызывает его перед `publish_review`.
  - *Плюсы:* гибкость, можно обновлять мета по ходу.
  - *Минусы:* лишний round-trip, больше точек отказа.
- **C. Автособирать на сервере:** сервер сам считает `started_at`/`duration_ms` от `prepare_review`, а usage/cost получает из хуков LLM-провайдера.
  - *Плюсы:* не нужно менять клиент.
  - *Минусы:* LLM-вызовы происходят в клиенте (Claude Code), сервер их не видит; не решает проблему.

**Выбор: A** — расширить `publish_review`. Это естественный контракт: клиент знает модель и usage, а сервер знает findings и статус публикации.

### 2. Серверный `review_steps`

- **A. Клиент передаёт готовый список steps в `publish_review`.**
  - *Плюсы:* клиент контролирует семантику этапов.
  - *Минусы:* клиент должен сериализовать шаги.
- **B. Сервер автоматически логирует вызовы MCP-инструментов (`search_code`, `get_related_symbols`, `submit_findings`, ...) в `_Session`.**
  - *Плюсы:* не нужно менять клиентские skills; шаги консистентны.
  - *Минусы:* ограниченная семантика (tool call без LLM-usage).
- **C. Гибрид:** сервер логирует tool calls, а клиент может дополнять high-level этапы.

**Выбор: B** как MVP — логировать вызовы инструментов в `_Session` и передавать их в `_record_history`. Это закрывает критерий "`review_steps` содержит записи по этапам/вызовам" без переделки всех клиентских skills.

### 3. Учёт sidechain-токенов в `brief_cost`

- **A. Включить `isSidechain=True` в основную сумму.**
  - *Плюсы:* просто.
  - *Минусы:* искажает картину — sidechain может быть не solve-task.
- **B. Искать sidechain-вызовы с маркерами solve-task и суммировать отдельно, отображая в блоке как "в т.ч. sidechain: X".**
  - *Плюсы:* точнее, сохраняет прозрачность.
  - *Минусы:* нужно знать формат JSONL для sidechain.
- **C. Добавить пометку в бриф о возможной недооценке.**
  - *Плюсы:* дёшево.
  - *Минусы:* не решает проблему.

**Выбор: B** — реализовать раздельный подсчёт, если формат JSONL позволяет; иначе fallback на C с явной документацией.

### 4. Auto permission mode в Step 1.5

- **A. Добавить в skill проверку auto mode (переменная окружения/флаг) и молча выбирать mid tier.**
- **B. Всегда использовать mid tier, убрав вопрос.**
- **C. Вынести default tier в `.review.yml`.

**Выбор: A** — сохранить вопрос в ручном режиме, но в auto mode делать silent fallback на mid tier. Детекция auto mode зависит от CLI; если невозможна — fallback на mid tier.

### 5. Guard/alert на зазор в истории

- **A. CLI/API diagnostic `reviewer history-gap <repo>`** — показывает дни с последнего прогона.
- **B. Alert в web admin dashboard.**
- **C. Warning в `prepare_review`/`publish_review`, если для репо нет прогонов за N дней.

**Выбор: A** — добавить метод в `ReviewHistory` и endpoint/CLI для диагностики. Это не ломает основной путь и даёт явный сигнал.

## Детальный дизайн

### 1. `publish_review` с метаданными

В `reviewer/mcp/service.py`:

```python
def publish_review(
    self,
    repo: str,
    pr: int,
    summary: str,
    dry_run: bool = False,
    task_key: str | None = None,
    *,
    model: str | None = None,
    model_verify: str | None = None,
    usage: dict | None = None,
    total_cost: float | None = None,
    started_at: datetime | None = None,
    steps: list[dict] | None = None,
) -> dict:
```

- `started_at` — ISO-строка или datetime от клиента; `finished_at` и `duration_ms` вычисляются в `_record_history`.
- Если `model` не передан — fallback на `"claude-code"` для обратной совместимости.
- `usage` сериализуется в JSONB в `ReviewHistory.record_run`.
- `steps` добавляются к серверным steps.

В `_record_history`:
- `started_at = s.started_at or now`
- `finished_at = now`
- `duration_ms = int((finished_at - started_at).total_seconds() * 1000)`
- `model = model or "claude-code"`

`_Session` получает поле `started_at: datetime` при `prepare_review`; если сессия была регидрирована из Postgres, `started_at` может отсутствовать — fallback на `now`.

### 2. Серверный `review_steps`

В `_Session` добавляем `steps: list[dict]`. В `_invoke_tool` перед вызовом/после вызова инструмента добавляем запись:

```python
{
    "stage": "analyze",  # или "verify", "synthesize"
    "unit": args.get("path") or args.get("node_id") or "",
    "seq": len(s.steps),
    "kind": "tool_call",
    "name": name,
    "text": result[:500] if isinstance(result, str) else None,
    "tool_calls": [{"name": name, "args": args}],
    "tokens": 0,
    "cost": 0.0,
}
```

Stage определяется по имени инструмента/состоянию сессии:
- `search_code`, `get_related_symbols`, `read_file`, ... → `"analyze"`
- `get_candidate_findings`, `submit_verdicts` → `"verify"`
- `publish_review` → `"synthesize"`

В `_record_history` передаём `s.steps + (steps or [])`.

### 3. Учёт sidechain-токенов

В `plugin/hooks/brief_cost.py`:

- `aggregate_usage` учитывает записи `isSidechain=True`, но только если в цепочке сообщений рядом есть маркеры solve-task.
- Результат разбивается на два bucket'а: основной и sidechain.
- `render_block` выводит дополнительную строку: "В т.ч. sidechain-сабагент: X токенов".

Если формат JSONL для sidechain недоступен/неизвестен — fallback на документирование ограничения:
- Добавить в блок строку: "*Sidechain-токены сабагента могут не учитываться — см. ограничение.*"

### 4. Auto permission mode в solve-task

В `plugin/skills/solve-task/SKILL.md` Step 1.5 заменить безусловный вопрос на:

> "Если активен auto permission mode — выбрать mid tier (Sonnet-class) и не задавать вопрос. Иначе — спросить пользователя."

Для Codex CLI auto mode может детектироваться по переменной окружения `CODEX_AUTO_APPROVE` или аналогичной. Если детекция невозможна — всегда fallback на mid tier.

### 5. Guard на зазор в истории

В `reviewer/web/history.py` добавить метод:

```python
def days_since_last_run(self, repo: str) -> int | None:
    ...
```

В `reviewer/web/api.py` добавить endpoint `/api/runs/gap` или поле в `/api/stats`.
В CLI `reviewer` добавить команду `history-gap` (опционально, если scope позволяет).

## Тестирование

- **Unit:** обновить `tests/mcp/test_publish.py` — проверить, что `history.runs[0]` содержит `model`, `duration_ms>0`, `steps` при передаче мета.
- **Unit:** `tests/hooks/test_brief_cost.py` — новый тест на sidechain-токены.
- **Unit:** `tests/skills/test_solve_task_brief.py` (или аналог) — guard-тест на auto-mode fallback.
- **Integration:** `tests/web/test_history.py` — проверить end-to-end `publish_review` с steps.

## Ошибки и отказоустойчивость

- Все изменения fail-open: если метаданные не переданы — fallback на текущие заглушки.
- Если `steps=None` — серверные steps всё равно пишутся.
- Если sidechain-формат не распознан — блок токенов не ломается, добавляется пометка.
- Guard на зазор — read-only, не блокирует ревью.

## Открытые вопросы

1. Формат JSONL sidechain-вызовов в текущем Claude Code / Codex — нужно подтвердить наличие поля `isSidechain` и структуру `message`.
2. Какой env-переменной/флагом детектировать auto permission mode в каждом CLI (Codex, Claude Code, Gemini, Cursor, Kimi)?
3. Нужно ли передавать `usage`/`total_cost` из клиентских skills review-pr/maintainability-review/performance-review тоже, или только из solve-task?

## Связанные задачи

- PRI-208 — выбор модели для сборки брифа (PR #97, не вмержен в dev на момент брифа).
- PRI-131 — Why-trace на каждом комментарии (не пересекается по scope).
