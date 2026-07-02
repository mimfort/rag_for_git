# PRI-203 — Reviewer-грунтовка за пределами брифа (план/ревью) — Design

- **Задача:** PRI-203 — https://ru.yougile.com/team/686c049c8af8/#PRI-203
- **Бриф:** `docs/superpowers/briefs/2026-07-02-PRI-203-reviewer-rag-beyond-brief.md`
- **Дата:** 2026-07-02

## Контекст / проблема

Reviewer (RAG-поиск + граф кода) задействуется только в `solve-task` — при сборке брифа.
Дальше по конвейеру `задача → бриф → спека → план → PR` его тулы не используются: планирование,
ревью и реализация работают на «сыром» grep/Read. Класс риска — **пропущенный call-site при смене
сигнатуры** (кросс-таск регрессия, ловится поздно).

**Неочевидный факт, переопределяющий форму задачи:** session-less тулы (`search_codebase`,
`callers`, `related_symbols`, `definition`) — это MCP-тулы, **уже доступные в любой фазе**, пока
reviewer-MCP подключён. Значит задача — не «дать доступ», а **durable-подтолкнуть использовать** там,
где окупается. Единственное препятствие: скилы `writing-plans` / `brainstorming` / официальный
`/code-review` **вендоренные** (`~/.claude/plugins/cache/.../superpowers/6.1.0/`), вне репозитория —
их текст править нельзя (перезатрётся апдейтом, не в репо, не шипится команде). Плюс плагин ставится в
**чужие** репозитории под **разные** клиенты (install.py шьёт CLAUDE.md / AGENTS.md / GEMINI.md /
Cursor / Codex…), поэтому правка CLAUDE.md *этого* репо не помогает пользователям плагина.

## Цель

Дать пользоваться reviewer-инструментами за пределами брифа — **точечно**, fail-open, без
форсирования во всех фазах. Владеемые поверхности чиним кодом; вендоренные фазы включаются **opt-in
блоком в README**, который пользователь по желанию вставляет в свой контекст-файл.

## Не-цели (out of scope)

- Working-tree overlay ретрива (свежесть на своих правках) — отдельная большая задача.
- Форсирование reviewer во всех фазах и в реализации субагентами.
- Движок retrieval / cliff / ANN (сделано в PRI-202) — не трогаем.
- Правка вендоренных superpowers-скилов и официального `/code-review`.

## Решения (резолвим открытые вопросы задачи)

1. **Session-less impact-тул движка — НЕ делаем.** `callers` / `related_symbols` уже дают
   session-less blast-radius. Signature-gated impact (`compute_impact`) требует head-vs-base overlay,
   которого для локального WIP нет (working-tree overlay — вне скоупа). `get_impact` остаётся
   PR-session-only. → закрывает критерий «решён вопрос session-less impact-тула».
2. **План частично грунтуется уже сейчас** через бриф `solve-task` (grounded `path:line` +
   blast-radius сидируют план). README-конвенция докрывает символы, всплывшие уже в ходе планирования.
3. **Реализацию субагентами не грунтуем** (YAGNI — «точечно, не везде»).
4. **Механизм для вендоренных фаз — README-блок (EN+RU), а не скилл-автозаписыватель** контекст-файлов
   (проще, клиент-агностично, opt-in) и не CLAUDE.md этого репо (не шипится пользователям плагина).

## Компоненты

### 1. `plugin/skills/_common/reviewer-grounding.md` (новый общий reference-блок)

Единственный источник конвенции грунтовки; подключается ревью-скилами маркером
`<!-- include: _common/reviewer-grounding.md -->`. **Без вложенных include** (инвариант guard-теста).
Содержимое (verbatim, на английском — как прочие `_common/*.md`):

```
Reviewer grounding (optional, fail-open):

When the reviewer MCP server is connected AND its base index is fresh, prefer the
session-less reviewer tools over raw grep/Read to ground cross-file facts — but only
where it pays. When reviewer is absent or the index is stale, silently fall back to
grep/Read; the standalone baseline is unchanged.

- Freshness check (once): `reviewer status <repo-path> --branch <branch> --json`.
  `drift == 0` → fresh, use the tools; `drift > 0` → stale, note it and keep going on
  the stale index (do NOT reindex mid-task); `drift == null` or the command fails →
  no index, fall back to grep/Read.
- Tools: `search_codebase(repo, query, branch?)` — find relevant code by description;
  `callers(repo, node_id, branch?)` — blast-radius: who calls a symbol whose signature
  you are about to change; `related_symbols(repo, node_id, branch?)` — graph neighbours;
  `definition(repo, symbol, branch?)` — where a symbol is defined. `node_id` is `path#fqn`;
  `search_codebase` snippets are headed by it, so feed that id to the graph tools.
- Targeted, not everywhere: skip grounding for small or familiar edits and for files
  already in context — grep is cheaper and Voyage is rate-limited (3 RPM / 10K TPM).
  Reach for reviewer when a change crosses files or touches a shared signature.
- Honesty about freshness: the base index tracks the target branch (base:<branch>),
  NOT your working tree; there is no working-tree overlay for local WIP. Grounding is
  reliable for facts about existing code (planning, callers of an unchanged symbol);
  it is blind to symbols you just edited locally — verify those with Read.
```

### 2. Грунтовка собственных ревью-скилов (владеемое, автоматически у всех)

Файлы: `plugin/skills/maintainability-review/SKILL.md`, `plugin/skills/performance-review/SKILL.md`.

Сейчас оба включают `_common/tool-usage.md` и говорят «Use the PR-session tools above», а в Method
ссылаются на PR-session тулы (`read_file`, `search_code`, `find_callers`), которых в **standalone**-
режиме **нет** → грунтовки нет, только disk/grep. Это баг.

Правка (обе скилы, симметрично):
- После `<!-- include: _common/tool-usage.md -->` добавить `<!-- include: _common/reviewer-grounding.md -->`.
- Заменить строку «Use the PR-session tools above.» на пояснение двух режимов: *в `/reviewer_review-pr`*
  — PR-session тулы; *standalone* — session-less тулы по правилам блока reviewer-grounding, иначе grep/Read.
- В Method-шаге про «open nearby code» добавить: в standalone при свежем reviewer использовать
  `search_codebase` / `callers` для соседнего кода и blast-radius; fail-open в disk/grep.

`review-pr` (analyze/verify/blast-radius/requirements-промпты) уже грунтован PR-session тулами
(включая `get_impact`) — **не трогаем**. `ask` / `solve-task` уже используют session-less — не трогаем.

### 3. Opt-in блок в README (EN + RU)

Новый раздел в корневом `README.md` (после `## Configuration reference` либо перед `## CLI reference`)
+ короткий указатель-ссылка в `plugin/README.md` (раздел «Установка плагина» / новый пункт). Даём
**обе** языковые версии копипаст-блока — пользователь вставляет подходящую в свой контекст-файл
(CLAUDE.md / AGENTS.md / GEMINI.md / .cursorrules — по своему клиенту).

Раздел README (EN), с копипаст-блоком:

```
## Reviewer grounding in plan/review phases (optional)

The reviewer MCP tools are available in every phase, not only inside a PR review. If you
run a plan/review workflow (e.g. Superpowers' writing-plans, or any code-review step), you
can have the agent ground its work in the RAG + code graph instead of raw grep. This is
opt-in: paste the block below into your agent context file (CLAUDE.md / AGENTS.md /
GEMINI.md / .cursorrules — whichever your client uses).

> **Reviewer grounding (plan/review, optional, fail-open).** When the reviewer MCP is
> connected and its base index is fresh (`reviewer status --json` → `drift == 0`), prefer the
> session-less reviewer tools over grep to ground cross-file facts during planning and review:
> `search_codebase` (relevant code), `callers` (blast-radius of a signature you are about to
> change), `related_symbols`, `definition`. Be targeted — skip small/familiar edits and files
> already in context (Voyage is rate-limited). The base index tracks the target branch, not
> your working tree: grounding is reliable for existing code but blind to symbols you just
> edited locally — verify those with Read. If reviewer is absent or the index is stale, fall
> back to grep/Read.
```

RU-версия раздела (заголовок «## Грунтовка reviewer в фазах план/ревью (опционально)»), с
копипаст-блоком:

```
## Грунтовка reviewer в фазах план/ревью (опционально)

Тулы reviewer-MCP доступны в любой фазе, не только внутри ревью PR. Если вы работаете по
конвейеру план/ревью (например, writing-plans из Superpowers или любой шаг code-review),
можно заставить агента грунтовать работу в RAG + графе кода вместо голого grep. Это opt-in:
вставьте блок ниже в свой контекст-файл (CLAUDE.md / AGENTS.md / GEMINI.md / .cursorrules — по
вашему клиенту).

> **Грунтовка reviewer (план/ревью, опционально, fail-open).** Когда reviewer-MCP подключён и
> его base-индекс свеж (`reviewer status --json` → `drift == 0`), в фазах планирования и ревью
> предпочитай session-less тулы reviewer голому grep для кросс-файловых фактов: `search_codebase`
> (релевантный код), `callers` (blast-radius сигнатуры, которую собираешься менять),
> `related_symbols`, `definition`. Точечно — пропускай мелкие/знакомые правки и файлы, уже в
> контексте (Voyage rate-limited). Base-индекс отслеживает целевую ветку, не твоё рабочее дерево:
> грунтовка надёжна для существующего кода, но слепа к символам, которые ты только что правил
> локально — их проверяй через Read. Если reviewer недоступен или индекс устарел — откат в grep/Read.
```

### 4. Догфуд в CLAUDE.md этого репо

Вставить RU-вариант opt-in блока (из компонента 3) в `CLAUDE.md` этого репозитория (новый раздел,
напр. после «## Неочевидные факты» либо «## Соглашения») — чтобы rag_for_git сам стал пользователем
конвенции и грунтовка включилась при разработке самого плагина. Валидирует блок на живом.

## Как это работает по фазам (data flow)

- **solve-task (уже есть):** бриф грунтован (`search_codebase` + граф) → сидирует план. Без изменений.
- **writing-plans / brainstorming (вендор):** включаются через opt-in блок в контекст-файле
  пользователя → агент точечно зовёт session-less тулы для Files/Interfaces + blast-radius; fail-open.
- **Ревью standalone (владеем):** `maintainability-review` / `performance-review` сами используют
  session-less тулы при свежем reviewer; иначе grep/Read.
- **Ревью PR (владеем, уже есть):** `review-pr` грунтован PR-session тулами. Без изменений.
- **Реализация субагентами:** не грунтуем.

## Тестирование

Guard-тесты `tests/skills/`:
- Новый `_common/reviewer-grounding.md` должен проходить инварианты общих блоков (существует, непустой,
  без вложенных `<!-- include: -->`). Если тест перечисляет блоки списком — добавить его; если глобит
  каталог — покрыт автоматически.
- Ассерт: собранные промпты `maintainability` и `performance` содержат маркер/текст reviewer-grounding
  (по образцу существующих проверок сборки промптов).
- Изменений движка нет → серверных / integration-тестов не добавляем. Прогнать `ruff check` на
  изменённых файлах и `pytest -q tests/skills`.

## Критерии приёмки (маппинг на задачу)

- [x] Конкретные точки вставки: собственные ревью-скилы (код) + opt-in README-блок для вендоренных
  план/brainstorm-фаз, с fail-open деградацией в grep/Read.
- [x] Вопрос session-less impact-тула решён: **не делаем**, `callers` достаточно; зафиксировано.
- [x] Политика «точечно, не везде» + ограничение свежести (WIP vs base) — в `_common/reviewer-grounding.md`
  и в README-блоке.
- [x] Standalone-baseline и solve-task не ломаются: грунтовка строго opt-in / fail-open, дефолт — grep/Read.

## Директива модели

Реализация субагентами — на **Opus** (переопределяет дефолт «код → Sonnet»). Перенести в
writing-plans / subagent-driven-development.
