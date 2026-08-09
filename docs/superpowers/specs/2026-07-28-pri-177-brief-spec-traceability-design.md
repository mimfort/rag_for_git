# Дизайн — Трассируемость brief → spec в solve-task (PRI-177, урезанный скоуп)

Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-177
Бриф: `docs/superpowers/briefs/2026-07-28-PRI-177-brief-spec-traceability.md`

## 1. Проблема

Шаг 5 скилла `solve-task` передаёт бриф в `superpowers:brainstorming` как путь к файлу, но ничего
не просит записать о происхождении в получившейся спеке. Результат — трасса
задача→бриф→спека→PR держится на привычке автора, а не на инструкции: из 25 июльских спек
**20 ссылаются на бриф, 5 — нет**. Шаг 5 при этом **не покрыт ни одним тестом**
(`grep -n 'brainstorming\|handoff' tests/skills/*.py` пуст), поэтому правка шага может молча его
сломать.

### 1.1 Почему скоуп урезан относительно исходной задачи


- Собственный пример задачи, бриф `2026-06-27-solve-task-brief-token-cost.md` → спека
  `2026-06-27-solve-task-brief-token-cost-design.md`: все 6 constraints присутствуют, каждый
  отдельной секцией (атрибуция окна → §5.1, дистрибуция хука → §4.1, цена/кэш → §1.1 + §5.2,
  идемпотентность → §7.1, контракт stdin → §8, семантика «без плагина» → §1 + §2).
- Бриф `2026-07-20-PRI-212-session-keepalive.md` → спека `2026-07-20-pri-212-session-keepalive-design.md`:
  инвариант GC, единая семантика TTL, отказ от бампа `created_at` разошлись по `## Цели`,
  `## Не-цели`, `## Решение`.

Constraints брифа — это открытые вопросы **до** дизайна; спека их **закрывает**. Копия verbatim
положила бы в спеку вопрос рядом с разделом, где ответ уже дан, — документ противоречил бы сам
себе. Плюс бюджет в задаче занижен ×5–10 (оценка +100–200 токенов против фактических ≈700 у
PRI-212 и ≈1200 у PRI-217) и возникает конфликт с PRI-175, который вводит в Constraints брифа
теги этапа сбора контекста (`[index_stale]`, `[boardless]`), бессмысленные в спеке.

Остаётся ровно та часть, которая подтверждается данными: **provenance-ссылка**.

## 2. Решение

Одна правка первого абзаца шага 5 в `plugin/skills/solve-task/SKILL.md` и один guard-тест.

**Маркер не вводится.** Якорь трассы — сам путь `docs/superpowers/briefs/…md`: он уникален,
грепается (`grep -l 'docs/superpowers/briefs/' docs/superpowers/specs/*.md` уже сейчас находит
все 26 спек из 83, где ссылка есть, — включая те 20 из 25 июльских) и извлекается регуляркой `docs/superpowers/briefs/[^\s\`)]+\.md`. Выделенный
машинный якорь вида `<!-- brief: … -->` отклонён: он дублирует путь в двух местах (рассинхрон —
вопрос времени) и вносит вопрос локализации в глобальный плагин, ничего не добавляя к грепу.
Прецеденты `<!-- ai-review:hash -->` и `<!-- reviewer:task-link -->` в проекте решают другую
задачу — идемпотентность перезаписи; спека пишется один раз, перезаписывать нечего.

**Язык и формат строки не фиксируются.** Плагин глобальный: наши спеки продолжат писать
`Бриф:` (так уже в 8 спеках), англоязычное репо напишет `Brief:` — грепается по пути в обоих
случаях.

## 3. Правка `plugin/skills/solve-task/SKILL.md`

Заменяется **только первый абзац** шага 5 (текущие строки 265–270). Блоки
«After the PR is created» и «Board-less mode» ниже — без изменений. Текст английский, как весь
`SKILL.md` (скилл при этом продолжает инструктировать общаться с пользователем по-русски).

```markdown
5. **Hand off to development.** Show the brief, state the saved file path
   (`docs/superpowers/briefs/…`), then invoke `superpowers:brainstorming` with the brief **file
   path** as the seed/context — so the brief survives compaction, not just the in-context text.
   **Ask brainstorming to record the brief's provenance in the spec:** one line under the spec
   heading pointing at the brief's path (`docs/superpowers/briefs/…md`), in the spec's own
   language — the path itself is the greppable anchor for the задача→бриф→спека→PR trace, so no
   dedicated marker is needed. Do NOT ask it to copy the brief's `## Constraints / open
   questions` verbatim: those are open questions brainstorming exists to RESOLVE, and a verbatim
   copy would contradict the very spec that answers them.
   From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.
```

## 4. Guard-тест `tests/skills/test_solve_task_brief.py`

Один новый тест плюс модульная константа с регуляркой. `re` в файле уже импортирован.

```python
STEP5_RE = re.compile(
    r"^5\. \*\*Hand off to development\.\*\*(.*?)^## Failure handling", re.S | re.M
)


def test_solve_task_step5_asks_for_brief_link_not_verbatim_constraints():
    """PRI-177 (урезанный скоуп): Step 5 требует provenance-ссылку на бриф в спеке
    и явно запрещает копировать Constraints verbatim."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    m = STEP5_RE.search(text)
    assert m, "Step 5 (hand off) не найден — заголовок шага переименован"
    step5 = m.group(1)
    assert "docs/superpowers/briefs/" in step5   # путь брифа = якорь трассы
    assert "provenance" in step5.lower()         # просим записать происхождение
    assert "Do NOT ask it to copy" in step5      # явный запрет
    assert "verbatim" in step5
```

**Почему region-scoped, а не whole-file как остальные тесты файла.** Это осознанное отступление
от домашнего стиля, вызванное конкретным риском холостого прохода: `Constraints` встречается в
`SKILL.md` 5 раз (строки 38, 67, 71, 234, 257), `docs/superpowers/briefs/` — 6 раз (241, 249,
254, 259, 267, 294). Whole-file-ассерт остался бы зелёным даже при полностью удалённом шаге 5.
Формулировки тест не пинит — только стабильные маркеры, как требует докстринг файла.

## 5. Порядок работ (TDD, red first)

1. Добавить `STEP5_RE` и тест → прогнать, убедиться, что падает на `"provenance" in step5.lower()`
   (сейчас в шаге 5 этого слова нет). **Red.**
2. Применить правку абзаца в `SKILL.md`. **Green.**
3. `.venv/bin/pytest tests/skills/ -q` — весь пакет guard-тестов скиллов зелёный
   (правка не должна задеть `test_assembled_prompts.py::test_solve_task_assembled_has_branch_and_tools`).
4. `python scripts/update_codex_plugin_manifest.py` — правка контента под `plugin/` меняет codex
   payload-digest; без пересборки манифестов падают install-тесты.
5. `.venv/bin/ruff check tests/skills/test_solve_task_brief.py`.

## 6. Не-цели

- **Ретрофит 5 июльских спек без ссылки** — историю не переписываем.
- **Фиксация формата/языка строки в спеке** — плагин глобальный.
- **Правка README.md / README.ru.md** — это нюанс промпта, а не фича; описание пайплайна
  solve-task в обоих README остаётся верным.
- **Carry-forward в `writing-plans`** и **правка `brainstorming` SKILL.md** — вне нашего репо
  (так же и в исходной задаче).
- **Verbatim-копия Constraints** — отклонена, обоснование в §1.1.

## 7. Открытый вопрос для доски

Описание PRI-177 на доске требует секции `## Brief` и guard-ассерта `"copy verbatim"`, что прямо
противоречит принятому решению. Задачу нужно переформулировать под сокращённый скоуп либо
закрыть с комментарием о ложной посылке — решается вне этого дизайна.
