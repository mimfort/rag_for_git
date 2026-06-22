# PRI-119 — PR walkthrough (гид по чтению для ревьюера-человека) — дизайн

**Задача:** PRI-119 (ID-119), оценка S–M. Слой: Плагин/агент.
**Ветка работы:** `feat/graphrag-summaries-walkthrough`.
**Зависимость:** усиливается PRI-159 (subsystem-summaries как приор), но **работает и без них**.
Реализуется **после** PRI-159 (см. `2026-06-23-pri-159-community-summaries-design.md`).

## Проблема

Нужно помочь ревьюеру-**человеку** ориентироваться в PR (а не искать баги): откуда начать читать,
что меняет каждый файл, на что это влияет. Это отдельный результат, не связанный с находками-багами
(`review-pr`).

**Критерий приёмки (из задачи):** на реальном PR выдаёт осмысленное «начни отсюда» + список
«осторожно, влияет на X».

## Решения (зафиксированы на brainstorming)

- Новый **скилл** `plugin/skills/pr-walkthrough/SKILL.md` (отдельный артефакт от находок-багов),
  переиспользующий существующую PR-сессию и графовые тулы. Нового Python — минимум (опц. один метод
  постинга).
- Вывод — **markdown-гид**, по умолчанию в терминал; постинг в PR — **опционально и с подтверждением**
  (outward-facing). Отдельно от находок-багов.
- Приор подсистем (PRI-159 `get_subsystem_summaries`) — **опционально, fail-open**.

## Архитектура и поток данных

```
skill ← PR number/URL (+ repo)
  1. prepare_review(repo, pr)               [есть] → units, patches, changed_node_ids, changed_paths
                                                     (ветка вне REVIEW_BRANCHES → status="skipped")
  2. get_impact(repo, pr)                   [есть] → затронутые символы + вызывающие/центральность
  3. get_changed_file_diff(path) + units    [есть] → «что меняет каждый файл»
  4. find_callers / get_related_symbols      [есть] → «осторожно, влияет на X» (blast radius)
  5. get_subsystem_summaries(repo, branch)  [PRI-159, опц.] → назвать затронутые подсистемы (fail-open)
  6. LLM собирает markdown-гид:
       «Начни отсюда» (порядок по центральности из get_impact) →
       по файлу: 1-строчное «что меняет» →
       «Осторожно, влияет на: …» (вызывающие)
  7. Вывод в терминал (дефолт). Опц. постинг в PR — только по явной просьбе + подтверждение.
```

Все тулы шагов 1–4 уже существуют (PR-session MCP-тулы: `prepare_review`, `get_impact`,
`get_changed_file_diff`, `find_callers`, `get_related_symbols`, `read_file`, `get_definition`).
Порядок чтения по центральности строит LLM из вывода `get_impact` — отдельного Python-хелпера не
требуется (держим скилл лёгким, S–M).

## Компоненты

| Компонент | Тип | Роль |
|---|---|---|
| `plugin/skills/pr-walkthrough/SKILL.md` | новый | Скилл-гид (тело EN, вывод RU; `_common`-include'ы) |
| `reviewer/mcp/service.py` | правка (опц.) | `post_pr_walkthrough(repo, pr, markdown)` — постинг гида в PR |
| `reviewer/entrypoints/mcp_server.py` | правка (опц.) | `@mcp.tool()`-обёртка над методом выше |

## Контракты

### Скилл `plugin/skills/pr-walkthrough/SKILL.md`

- Тело — на английском (токены); **гид и вывод пользователю — на русском** (язык проекта).
- Inputs: номер PR / URL (+ repo из git remote, как в `review-pr`).
- Резолв repo/branch (include `_common/branch-selection.md`), tool-usage (include
  `_common/tool-usage.md`), anti-hallucination (include `_common/anti-hallucination.md` — каждое «влияет
  на X» подтверждено `find_callers`, без выдумок).
- Шаги 1–6 выше. На `prepare_review → {"status":"skipped"}` (ветка не отслеживается) — сообщить и выйти.
- Структура гида (markdown):
  - **Начни отсюда** — упорядоченный список файлов/символов по центральности (`get_impact`).
  - **По файлам** — на каждый изменённый файл одна строка «что меняет».
  - **Осторожно, влияет на** — затронутые символы и их вызывающие (`find_callers`).
  - (опц.) **Подсистемы** — 1–2 строки из `get_subsystem_summaries`, если доступны.
- Вывод: по умолчанию печать гида в терминал/ответ. Постинг в PR — только если пользователь явно
  попросил, и после подтверждения (outward-facing).

### (опц.) `MCPReviewService.post_pr_walkthrough(repo, pr, markdown) -> dict`

- Использует активную сессию (`prepared.prq.head_sha`, `prepared.vcs`); постит **review** с
  `body=markdown`, `event="COMMENT"`, `comments=[]` (переиспользует
  `GitHubProvider.publish_review(number, head_sha, summary=markdown, comments=[])`).
- **Идемпотентность:** в `body` зашит скрытый маркер `<!-- ai-walkthrough -->` (отдельный от
  `<!-- ai-review:* -->` находок) — повторный прогон не путается с ревью-комментариями.
- Возвращает `{posted: true, pr}`. Fail-soft при сетевой ошибке.
- Если сессии нет (не было `prepare_review`) → понятная ошибка с recovery hint (как у session-тулов).

## Обработка ошибок / краевые случаи

- Граф недоступен → `get_impact`/`find_callers` деградируют; порядок чтения — по файлам диффа (fail-open).
- PRI-159 не построен / summary пусто → раздел «Подсистемы» опускается (fail-open).
- Ветка PR вне `REVIEW_BRANCHES` → `prepare_review` вернёт `skipped` (как `review-pr`).
- Постинг в PR никогда не выполняется без явной просьбы пользователя.

## Тестирование

- **Guard-тест** сборки промпта скилла (`tests/skills/` — раскрытие `_common`-include'ов).
- **Unit** (если добавляем `post_pr_walkthrough`): постинг через фейковый VCS — проверить
  `body`-маркер `<!-- ai-walkthrough -->`, пустой список inline-комментариев, `event="COMMENT"`
  (зеркало существующих publish-тестов).
- Ручная проверка на реальном PR: гид содержит «начни отсюда» + «осторожно, влияет на X».

## Вне объёма (YAGNI / на потом)

- Python-хелпер упорядочивания по центральности (LLM строит из `get_impact`).
- Автопостинг без подтверждения.
- Связка с PRI-112/PRI-115 (blast-radius/centrality усиления) — walkthrough работает на текущих тулах.
