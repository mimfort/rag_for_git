# Brief — configure-review подсказывает context_limits под конкретный репозиторий

## Task
Расширить скилл `reviewer_configure-review`, чтобы он рекомендовал per-repo значения блока
`context_limits` (PRI-202) в `.review.yml`, а не оставлял голые дефолт-константы. Сейчас скилл
тюнит только `summary_cluster_depth`/overrides/`summary_topk_threshold`/`paths.ignore`; блок
`context_limits` (search_codebase / search_tasks / graph) он не трогает вовсе.
Скоуп — **UX и эвристики скилла**, не движок retrieval (движок уже сделан в PRI-202).
Критерий: скилл выводит рекомендацию из измеримых характеристик репо и объясняет каждое число.

## Related work
- PRI-202 — движок адаптивных лимитов: cliff-отсечка + рельсы `[floor, ceiling]`, ANN-префильтр;
  `context_limits` читается **только** из `.review.yml`, env-слоя нет, отсутствие ключа = дефолт.
  Спека: `docs/superpowers/specs/2026-07-01-pri-202-configurable-context-limits-design.md` —
  оттуда ключевой ориентир «монорепа → ceiling 25-30» и что `top_k` теперь адаптивен (ceiling = рельса).
- PRI-168 — сам скилл configure-review: standalone, только git + правка файла, без reviewer MCP/БД.
- PRI-166 — эвристика `summary_cluster_depth` (скан структуры + churn): готовый шаблон, к которому
  пристраивается классификация «профиля репо».
- (dropped: PRI-161/167 — контекст-слой в целом, механизм понятен, отдельно не информируют; в
  корпусе `search_tasks` семантически близких задач не нашлось.)

## Subsystems
- `reviewer/policy` — `ReviewPolicy` + `ContextLimits.from_review_yaml`: как парсится блок (заданные
  ключи поверх дефолтов) — контракт, который должен эмитить скилл.
- `reviewer/config` — Settings: подтверждает, что у `context_limits` **нет** env-дефолта (только yml).
- `tests/skills` — guard-тесты структуры SKILL.md (must-mention ключи/шаги) — их надо дополнить.

## Relevant code
- `plugin/skills/configure-review/SKILL.md:87` — шаг 5 «Generate the recommended draft (heuristics)»:
  сюда добавляется подсекция `context_limits` + классификация профиля репо.
- `plugin/skills/configure-review/SKILL.md:66-79` — шаги 2-3 (git ls-tree скан структуры + churn):
  единственный источник сигналов; профиль репо строится из них, БД/Voyage недоступны.
- `plugin/skills/configure-review/SKILL.md:19-31` — Scope: перечень редактируемых ключей; дописать
  `context_limits` (иначе шаг 7 «never clobber outside scope» его запретит).
- `plugin/skills/configure-review/SKILL.md:2` — description во frontmatter: упомянуть context_limits.
- `reviewer/policy/context_limits.py:8-15` — дефолты и смысл каждой ручки (эталон значений/комментов).
- `.review.yml:59-82` (этой сессии) — уже добавленный документированный блок context_limits; служит
  образцом стиля, который скилл должен воспроизводить.
- `tests/test_review_yml_example.py:6` — `in`-проверки ключей эталонного .review.yml; логично
  добавить `assert "context_limits" in data`.

## Constraints / open questions
- **[git-only]** Скилл standalone (нет reviewer MCP / Postgres / Neo4j / Voyage). Все сигналы —
  из git: число `.py` файлов, структура top-level пакетов (моно- vs одиночный), churn. Плотность
  кода в чанках и размер доски задач напрямую **не измеримы** без БД.
- **Разбивка ручек по измеримости (главный дизайн-вопрос «как»):**
  - *Выводимы из git → авто-тюн:* `search_codebase.ceiling` + `candidate_pool` — от масштаба репо
    и монорепности (главный рычаг; монорепа/большой → ↑, крошечная утилита → ↓, экономия Voyage).
  - *НЕ выводимы из git → оставить дефолт, документировать как ручной advanced-тюн:* `ratio`,
    `abs_floor`, `ann_distance_max` (форма cliff/распределение скоров реранкера — скилл его не
    наблюдает); `graph.hops` (обрыв стоимости, почти всегда 1); `graph.callers_topk` (fan-in).
  - *Нужен сигнал, которого у standalone-скилла нет:* `search_tasks.ceiling` (размер доски). Варианты:
    (a) спросить пользователя, (b) оставить дефолт 8. Открытый вопрос.
- **[фундаментальное]** PRI-202 сделал `top_k` адаптивным через cliff — `ceiling` лишь предохранитель.
  Агрессивный тюн `floor/ratio` частично воюет с этой идеей. Сильнее всего оправдан узкий скоуп:
  править `ceiling`+`candidate_pool` только когда профиль репо явно нестандартный.
- **[минимализм конфига]** Отсутствие ключа = дефолт, поэтому эмитить блок `context_limits` стоит
  **только при отклонении** профиля от стандартного; стандартный репо → блок не добавлять (без раздувания).
- **[UX-развилка]** Точные 6 чисел из git «на глаз» vs 3 профиля-пресета («small util» / «standard
  service» / «large / monorepo») → бандл значений. Пресеты объяснимее и ложатся в поток «draft → правка».
- **[тесты]** Дополнить guard-тест `tests/skills/` (must-mention context_limits в SKILL.md) и
  `tests/test_review_yml_example.py` (ключ в эталоне). `.review.yml` этой сессии тесты не ломает (4 passed).
- **[existing_artifacts]** `docs/superpowers/briefs/2026-07-01-PRI-202-configurable-context-limits.md`
  — это бриф движка PRI-202, другой скоуп (не UX скилла); не перезаписывать.
- Индекс dev свежий (drift 0), корпус задач тёплый (93 задачи PRI), сводки построены.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 566 · out 26.5K · cache-write 288.2K · cache-read 2M
Всего: 2.3M токенов
