# Brief — Стоимость этапа solve-task в брифе (LLM-токены + $)

## Task
Board-less (задачи на доске нет — формулировка пользователя). В файл брифа, который пишет
скилл `solve-task`, опционально дописывать **стоимость этапа сбора контекста**: сколько
LLM-токенов потрачено и во сколько $ это обходится — чтобы потом сравнивать «с плагином
solve-task / без». Включается **флагом в `.review.yml`**, доп. работы по сбору — минимум.
Критерии (согласованы в обсуждении):
- Метрика: **4 бакета токенов + итоговый $** (fresh-in / output / cache-write / cache-read, затем цена).
- Механизм: **авто-хук `PostToolUse`** на запись брифа; **флаг читается хуком из `.review.yml`**
  (`solve_task.brief_token_cost: true`). Ноль доп. токенов LLM — считает детерминированный скрипт.
- Источник числа — транскрипт сессии, НЕ самооценка LLM (LLM не может надёжно сосчитать свои токены).

## Related work
(board-less, корпус задач не опрашивался — целенаправленно, фича харнес/плагин-уровня, не reviewer-кодовой базы). (dropped 0)

## Subsystems
(omit — приор сводок не привлекался; фича не в reviewer-ядре, а в plugin/хуке)

## Relevant code
- `plugin/.claude/settings.json` — сейчас только `permissions`; сюда регистрируется `PostToolUse`-хук (matcher `Write`, путь `docs/superpowers/briefs/*.md`), едет ко всем юзерам плагина.
- `plugin/.claude-plugin/plugin.json` — манифест плагина; проверить, нужна ли отдельная декларация хука (hooks.json) vs settings.json.
- `reviewer/policy/policy.py:31,71` — `.review.yml` парсится `yaml.safe_load`+`data.get`; новый ключ `solve_task` игнорируется server-side ⇒ Python reviewer НЕ трогаем, хук парсит yaml сам.
- `.review.yml` (корень репо) — добавить опциональный `solve_task.brief_token_cost: true` + комментарий; дефолт off.
- `plugin/skills/solve-task/SKILL.md` шаг 4 «Persist the brief» — точка-триггер: бриф пишется в `docs/superpowers/briefs/*.md`, это и ловит хук.
- Источник токенов: `~/.claude/projects/<repo>/<session>.jsonl` — на каждый ассистентский ход `message.usage` = {input, output, cache_creation, cache_read} + `model` + `timestamp` (проверено: 128 ходов в реальной сессии).
- Цены: реальные per-bucket ставки `claude-opus-4-8` подтянуть из skill `claude-api` (не из памяти).

## Constraints / open questions
- **Атрибуция окна этапа.** Старт = последнее user-сообщение `Base directory for this skill: …/solve-task`; конец = событие записи брифа. Несколько прогонов в сессии → брать последний старт. Субагентов до брифа нет (solve-task фанаутится только при handoff) → считаем main-loop.
- **Дистрибуция хука.** Плагинный путь (`plugin/.claude/settings.json` + `plugin/hooks/…`) = переиспользуемо всеми, но больше поверхности; vs только локальный `.claude/settings.json` этого репо.
- **Цена/кэш.** Бакеты стоят по-разному (cache_read ~10× дешевле fresh-in, output дороже всего) — поэтому $ считается по 4 ставкам, не по сырой сумме. Смешанные модели в окне → цена по каждой `model` и сумма. Риск устаревания прайс-карты.
- **Идемпотентность.** Повторный прогон перезаписывает бриф → хук должен ЗАМЕНЯТЬ блок `## Стоимость`, а не дописывать второй.
- **Контракт stdin хука.** `PostToolUse` отдаёт `tool_name`/`tool_input.file_path`/`transcript_path`/`cwd` — точные имена полей сверить до кодинга. `.review.yml` искать от `cwd`/git-toplevel.
- **Семантика «без плагина».** Этот замер = стоимость самого этапа solve-task; контрфактуал «холодного» решения без скилла пользователь меряет отдельно и сравнивает — авто-замер его не покрывает.
