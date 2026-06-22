# PRI-156 — Structured Outputs (schema-enforced JSON) для findings/verdicts

**Задача:** ID-156 / PRI-156 («Движок (reviewer CLI/MCP)»). Оценка M.
**Дата:** 2026-06-22. **Ветка базы:** `dev` (индекс досинхронизирован до HEAD `1cceefc`).

## Проблема

Субагенты ревью (analyze/dimension/verify) возвращают **free-text JSON**, который парсится
LLM-оркестратором. При malformed-выводе срабатывают хрупкие фолбэки: verify при кривом ответе
делает «KEEP all findings» (потеря фильтрации), а серверная коэрция `_finding_from_dict` чинит
кривые словари дефолтами постфактум. Это даёт парс-ошибки и расфокус калибровки.

**Цель:** перевести вывод analyze/dimension/verify на schema-enforced через **вызов MCP-тула**;
схему findings и verdicts определить **один раз**; валидацию вынести на тул-границу.

**Критерии приёмки (из задачи):**
- доля malformed-выводов → ~0;
- поля findings валидны по схеме;
- verify не теряет находки из-за парс-ошибок.

## Ключевое архитектурное ограничение

Субагенты — это **Claude Code LLM-субагенты**, которых диспатчит LLM-оркестратор через скилл
`review-pr/SKILL.md`. Это **не** прямые вызовы Anthropic API из Python, поэтому Python-сервер
**не может** выставить `response_format`/`tools` для энфорса схемы. Единственная граница, которую
Python реально контролирует, — это **вызов MCP-тула**. Отсюда механизм энфорса: субагент **вызывает**
MCP-тул, FastMCP/Pydantic валидирует аргументы по схеме, malformed → ошибка тула → субагент ретраит.

## Принятые решения (развилки brainstorming)

1. **Механизм энфорса:** новые MCP-тулы `submit_findings` / `submit_verdicts`. Субагент вызывает
   их вместо возврата free-text. Энфорс на тул-границе (FastMCP/Pydantic).
2. **Граница данных:** полностью через сессию. `submit_findings` присваивает каждой находке
   **server-assigned id**; verify читает кандидатов тулом и ссылается по id (не по хрупкому индексу
   массива); `publish_review` читает всё из сессии — **параметр `findings` убирается**.
3. **Источник схемы:** Pydantic-модели `FindingIn`/`VerdictIn` — **канонический** единый источник.
   FastMCP выводит из них тул-схему; внутренний dataclass `Finding` строится из валидированного
   `FindingIn`; `findings-schema.md` — тонкий человеческий док, который guard-тест сверяет с моделью.
4. **Строгость:** мягкая коэрция **внутри** `FindingIn`-валидаторов — поведение `_finding_from_dict`
   дословно (сохраняет PRI-144). Только `file` обязателен; остальное коэрцируется с дефолтами.
5. **Фолбэк verify:** отсутствие verdict = keep (структурный дефолт). Единственный путь отсева —
   явный `is_real=false`. Инструкция «KEEP all on malformed» из SKILL.md удаляется как избыточная.

## Связанный контекст (фундамент, оба смержены)

- **PRI-142** — `_common/findings-schema.md` уже существует (общий контракт). PRI-156 его **энфорсит**,
  не переопределяет.
- **PRI-144** — калибровка `confidence` (шкала 0.8–1.0 / 0.5–0.7 / ≤0.4) + коэрция (`confidence`
  fallback 0.1, clamp [0,1]). Семантику **сохранить** в валидаторах `FindingIn`.

## Архитектура

### Новые файлы / тулы

- **`reviewer/mcp/schemas.py`** (новый) — Pydantic-канон: `FixIn`, `FindingIn`, `VerdictIn`.
- **`reviewer/entrypoints/mcp_server.py`** — 3 новые `@mcp.tool()`-обёртки (типизированы Pydantic
  для энфорса): `submit_findings`, `submit_verdicts`, `get_candidate_findings`. Сигнатура
  `publish_review` меняется (без `findings`).
- **`reviewer/mcp/service.py`** — методы `submit_findings`/`submit_verdicts`/`get_candidate_findings`;
  расширение `_Session`; переработка `publish_review`; удаление `_finding_from_dict`.
- **`reviewer/vcs/base.py`** — `Finding.from_in(FindingIn) -> Finding`.

### Схема (`reviewer/mcp/schemas.py`)

```python
class FixIn(BaseModel):
    start_line: int | None = None     # coerce; мусор → None (вся fix отбрасывается выше)
    end_line: int | None = None
    replacement: str | None = None    # не-строка → None

class FindingIn(BaseModel):
    file: str                          # required → ValidationError, если нет
    category: str = "correctness"      # задаётся вызывающим скиллом
    severity: str = "medium"           # validator: вне {low,medium,high,critical} → "medium"
    side: str = "RIGHT"               # validator: вне {RIGHT,LEFT} → "RIGHT"
    line: int | None = None            # coerce; мусор → None
    code_quote: str | None = None      # не-строка → None
    message: str = ""
    suggestion: str | None = None      # не-строка → None
    confidence: float = 0.1            # validator: мусор → 0.1, clamp [0,1]
    fix: FixIn | None = None           # если start/end не коэрцятся или replacement не строка → None

class VerdictIn(BaseModel):
    id: str
    is_real: bool
```

Поведение валидаторов = дословный порт `_finding_from_dict` (service.py:49-106). `Finding.from_in(fi)`
строит dataclass `Finding` (+`centrality=0.0`); `fingerprint()` не меняется. Server-assigned id
кандидата живёт **в сессии**, не в `Finding`.

### Состояние сессии (`_Session`, service.py:111)

```python
candidates: dict[str, Finding]   # id → Finding (копится submit_findings)
verdicts:   dict[str, bool]      # id → is_real (копится submit_verdicts)
_seq:       int                  # счётчик для id вида "f{n}"
```

### Поведение тулов (методы `MCPReviewService`)

- **`submit_findings(repo, pr, findings: list[FindingIn]) -> dict`** — FastMCP уже провалидировал;
  для каждого `fi` строит `Finding.from_in(fi)`, присваивает id (`f{++_seq}`), кладёт в
  `candidates`. Возврат `{"accepted": N, "ids": [...]}`.
- **`get_candidate_findings(repo, pr) -> str`** — нумерованный/идентифицированный срез кандидатов
  для verify: `[{id, file, line, category, severity, message, code_quote}, …]`.
- **`submit_verdicts(repo, pr, verdicts: list[VerdictIn]) -> dict`** — пишет `verdicts[id]=is_real`;
  id вне `candidates` → игнор + `log.warning`. Возврат `{"recorded": N, "unknown_ids": [...]}`.
- **`publish_review(repo, pr, summary, dry_run=False, task_key=None) -> dict`** — читает
  `candidates`+`verdicts` из сессии. Отсев: `verdicts.get(id) is False` → drop; иначе keep. Далее без
  изменений: ground → snap → gate → dedup → centrality → assemble → publish → история → cleanup.
  `verify_rejected` = число `is_real=false`. `cleanup` чистит `candidates`/`verdicts` вместе с сессией.

### Поток данных

```
analyze/dimension субагент:
  → submit_findings(repo, pr, findings=[FindingIn, …])
        malformed → ошибка тула → РЕТРАЙ ; ok → candidates[id] = Finding.from_in(fi)
  → возвращает короткий статус-текст (не JSON)

verify субагент:
  → get_candidate_findings(repo, pr) → [{id, …}, …]
  → submit_verdicts(repo, pr, verdicts=[{id, is_real}, …])

оркестратор:
  → publish_review(repo, pr, summary, dry_run, task_key)   # без findings
        verdict is_real=false → drop; нет verdict / true → keep → … → publish
```

## Изменения промптов / скилла

- **`_common/findings-schema.md`** — интро «Return ONLY a JSON object» → «Submit findings via
  `submit_findings` with this per-finding schema»; поля без изменений (guard-тест сверяет с `FindingIn`).
- **`_common/dimension-output-tail.md`** — «верни JSON» → «вызови `submit_findings`».
- **`_common/tool-usage.md`** — добавить `submit_findings`/`submit_verdicts`/`get_candidate_findings`
  в список тулов.
- **`review-pr/references/verify-prompt.md`** — «Return ONLY {verdicts…}» → «прочитай
  `get_candidate_findings`, вынеси вердикты через `submit_verdicts(id, is_real)`».
- **`review-pr/references/analyze-prompt.md`** (+ dimension-промпты performance/maintainability/
  requirements/blast-radius через их output-контракт) — submit вместо возврата JSON.
- **`review-pr/SKILL.md`** шаги 3–6: субагенты submit'ят; verify читает кандидатов тулом; publish_review
  без `findings`; строка «If the verifier fails or returns malformed output, KEEP all findings» —
  **удаляется** (структурный дефолт).

## Обработка ошибок / фолбэк

- malformed-аргументы тула → FastMCP/Pydantic reject → ретрай субагента (энфорс; malformed → ~0).
- analyze-субагент умер без `submit_findings` → его юнит без находок → файл в «не проанализировано»
  (текущее поведение оркестратора сохраняется).
- verify умер / частичный → отсутствующие вердикты = keep (recall-safe, структурно).
- `submit_*` на неподготовленную сессию → ошибка тула «session not prepared» (как у прочих
  session-bound тулов).
- Пустые `candidates` → пустое ревью (валидно).

## Тестирование

- **`tests/mcp/`**: паритет коэрции `FindingIn` со старым `_finding_from_dict` (table-driven по тем
  же кейсам); `submit_findings` — аккумуляция + присвоение id; `get_candidate_findings` — формат;
  `submit_verdicts` — запись + unknown-id игнор; `publish_review` читает из сессии; verdict-absence =
  keep; частичный verify; счётчик `verify_rejected`.
- **`tests/skills/`**: guard — findings-schema.md ↔ `FindingIn` (вместо ↔ `Finding`); сборка промптов
  с submit-тулами; контракт verify-prompt.
- Обновить существующие тесты `publish_review` под новую сигнатуру (без `findings`).

## Совместимость

`publish_review` и новые submit-тулы вызывает **только** плагин `review-pr` этого репозитория —
внешних потребителей нет, смена сигнатуры безопасна. Проект на русском: комментарии/докстринги/CLI.
Guard-тесты `tests/skills/` держать зелёными.

## Вне скоупа (YAGNI)

- Прямые Anthropic API-вызовы с `response_format` (архитектура на LLM-субагентах этого не позволяет).
- Изменение шкалы калибровки `confidence` (это PRI-144, уже смержено).
- Переопределение полей схемы findings (это PRI-142; здесь только энфорс существующих полей).
- Хранение server-assigned id в самом `Finding` (id ephemeral, живёт в сессии).
