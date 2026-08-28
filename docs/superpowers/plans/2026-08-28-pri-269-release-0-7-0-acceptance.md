# PRI-269 — приёмка релиза 0.7.0: план прогона

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Прогнать 13 шагов приёмки релиза 0.7.0 на живой установке и зафиксировать наблюдаемые величины по 11 критериям в `eval/pri269_acceptance_report.md`, заведя отдельные задачи PRI на найденные дефекты.

**Architecture:** Шаги сгруппированы в фазы по требуемому состоянию инфраструктуры, чтобы переключать её четыре раза вместо девяти. Внутри первой фазы дорогой цикл ревью PR идёт рано — в приёмке PRI-236 блокер сидел именно там и вскрылся последним. Замеры снимаются двумя каналами: клиентским (MCP-тулы этой сессии — то, что видит пользователь) и серверным (изолированный процесс с `REVIEWER_ENV_FILE` — точное время и подмена конфигурации без порчи канонического `.env`).

**Tech Stack:** Python 3, `uvx --from rag-reviewer==0.7.0`, docker compose (ParadeDB 5433, Neo4j 7687), MCP-тулы reviewer, `gh` CLI.

**Spec:** `docs/superpowers/specs/2026-08-28-pri-269-release-0-7-0-acceptance-design.md`

## Global Constraints

- Репозиторий: `mimfort/rag_for_git`, ветка `dev`, клон `/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git`, рабочая ветка приёмки `feat/pri-269-release-0-7-0-acceptance`.
- Задача доски: `PRI-269` (нормализованный ключ `ID-323`), project `PRI`, тип доски `yougile`.
- Индекс `dev` на момент старта: `indexed_sha 27b3467`, `drift 0`, `summaries 40`, `chunks 7793`, `graph_nodes 8039`.
- Канонический env: `~/.config/rag-reviewer/.env`. Приоритет резолва: `$REVIEWER_ENV_FILE` → `~/.config/rag-reviewer/.env` → `./.env`.
- `pg_pool_max_size` по умолчанию `4`.
- Каждый замер пишется как **наблюдаемая величина** (секунды, значения `cause`/`remedy`, набор ключей payload, `retry_required`, watermark, расход Voyage), и лишь затем — вердикт. Отчёт из одних «ок» невоспроизводим.
- **Санкция человека обязательна перед:** остановкой ParadeDB/Neo4j, прогоном с подменённой конфигурацией (изолированный процесс, канонический `.env` не трогается), исчерпанием пула, созданием PR, `git push`, любой записью в доску (`create_task`, `finish_task`, `sync_board` в режиме записи).
- Шаг 8 задачи (стартовая панель, критерий 6) пройден 28.08.2026 — не переигрывать, только зафиксировать.
- Правки кода по найденным дефектам — **вне скоупа**: дефект фиксируется замером и заводится задачей PRI.

---

### Task 1: Предусловия, версии и скелет отчёта

**Files:**
- Create: `eval/pri269_acceptance_report.md`

**Interfaces:**
- Produces: файл отчёта с разделом на каждый из 11 критериев; последующие задачи дописывают в свои разделы.

- [x] **Step 1: Проверить установку 0.7.0 из PyPI**

```bash
uvx --from rag-reviewer==0.7.0 reviewer check 2>&1 | tail -20
```
Записать: список проверок и их статус. Ожидание — все строки `✓`, финальное «Готово к работе».

- [x] **Step 2: Снять версии плагина в обоих клиентах**

```bash
ls ~/.claude/plugins/cache/rag-reviewer-marketplace/rag-reviewer/
grep -n 'rag-reviewer' -A 4 ~/.codex/config.toml | head -20
```
Записать: установленные версии в Claude Code (ожидание — присутствует `0.7.0`) и версию/наличие блока в конфиге Codex.

- [x] **Step 3: Проверить целостность codex-манифеста**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
python scripts/update_codex_plugin_manifest.py --help 2>&1 | head -20
git diff --stat -- '*codex*'
```
Записать: изменяет ли скрипт манифест (чистый `git diff` = манифест актуален для текущей версии).

- [x] **Step 4: Создать отчёт со скелетом и предусловиями**

Создать `eval/pri269_acceptance_report.md`: заголовок, дата, версия, состав релиза, раздел «Условия прогона» (indexed_sha, drift, summaries, chunks, graph_nodes из Global Constraints), затем 11 пустых разделов «Критерий N — <формулировка>» и раздел «Найденные дефекты».

В раздел «Критерий 6» сразу записать зафиксированное: панель показана ровно одна, три вопроса, дословные заголовки `Brief model tier`, `Interaction mode`, `Execution strategy`, выбраны `mid`/`auto`/`auto`; источник — сессия сборки брифа 28.08.2026. Вердикт: пройден.

- [x] **Step 5: Коммит**

```bash
git add eval/pri269_acceptance_report.md
git commit -m "docs(pri-269): скелет отчёта приёмки и предусловия"
```

---

### Task 2: Полный цикл ревью PR (шаг 9, критерий 4) — дорогой сценарий первым

**Files:**
- Modify: `eval/pri269_acceptance_report.md` (раздел «Критерий 4»)

**Interfaces:**
- Consumes: отчёт из Task 1.
- Produces: замер публикации ревью; номер временного PR для закрытия в Task 7.

- [ ] **Step 1: Запросить санкцию на создание временного PR**

Спросить человека явно: «Создать временный PR с небольшой Python-правкой для шага 9? После прогона он будет закрыт без мержа.» Без явного согласия — задача останавливается, в отчёт пишется «не выполнено: нет санкции».

- [ ] **Step 2: Подготовить ветку с осмысленным .py-диффом**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
git checkout -b chore/pri-269-review-smoke
```

Внести правку в `reviewer/storage_health.py::is_loopback_endpoint` — функцию, которую приёмка и так изучает, так что диффу есть на что опираться в графе. Конкретно: вынести регулярное выражение `host=([^\s]+)` в модульную константу рядом с `_LOOPBACK_HOSTS` и использовать её в теле функции.

```python
_HOST_KV_RE = re.compile(r"host=([^\s]+)")
```

и в теле:

```python
    if host is None:
        match = _HOST_KV_RE.search(value)
        host = match.group(1) if match else None
```

Правка настоящая (компиляция регулярки уходит из горячего пути), но мержить её в рамках приёмки не нужно — PR закрывается в Task 7. Перед коммитом прогнать `.venv/bin/pytest -q tests/test_storage_health.py`, чтобы PR не был заведомо красным.

- [ ] **Step 3: Открыть PR (санкция получена в шаге 1)**

```bash
git push -u origin chore/pri-269-review-smoke
gh pr create --base dev --title 'chore: временный PR для приёмки PRI-269 (не мержить)' \
  --body 'Временный PR для шага 9 приёмки PRI-269. Не мержить — будет закрыт после прогона.'
```
Записать номер PR.

- [ ] **Step 4: Прогнать полный цикл ревью**

Вызвать скилл `/rag-reviewer:review-pr` на этом PR: `prepare_review` → анализ субагентами → `publish_review`. Замерить время каждой фазы.

Записать: время фаз; число опубликованных inline-комментариев; факт публикации сводки; `outcome`-распределение кандидатов, если доступно.

- [ ] **Step 5: Диагностический поиск для проверки RRF**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
.venv/bin/reviewer search "storage unavailable classification" --branch dev 2>&1 | head -30
```
Записать: первые результаты и их порядок. Ожидание — осмысленная выдача, ранжирование не выродилось (проверка, что параметризация `RRF_K` не задела SQL).

- [ ] **Step 6: Записать результат в раздел «Критерий 4» и закоммитить**

```bash
git add eval/pri269_acceptance_report.md
git commit -m "docs(pri-269): замер цикла ревью PR и диагностического поиска"
```

---

### Task 3: Живая инфраструктура — payload, eval replay, исчерпание пула (шаги 2, 10, 13)

**Files:**
- Modify: `eval/pri269_acceptance_report.md` (разделы «Критерий 4», «Критерий 7», «Критерий 10»)

**Interfaces:**
- Consumes: отчёт из Task 2.
- Produces: baseline времени ответа на живой инфраструктуре — точка сравнения для Task 4.

- [ ] **Step 1: Снять baseline времени и полноты payload (серверный канал)**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
.venv/bin/python - <<'PY'
import json, time
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

settings = Settings()
components = build_components(settings)
svc = MCPReviewService(settings, components)
t0 = time.perf_counter()
payload = svc.prepare_task_context("mimfort/rag_for_git", "PRI-269", "dev", warm_board=False)
print(f"elapsed_s={time.perf_counter() - t0:.2f}")
print("keys=", sorted(payload))
print("gaps=", json.dumps(payload["gaps"], ensure_ascii=False))
PY
```
Записать: `elapsed_s`, полный список ключей (ожидание — девять), содержимое `gaps` (ожидание — пусто).

- [ ] **Step 2: Записать критерий 4 (живая часть)**

В раздел «Критерий 4» добавить: `gaps: []`, девять ключей, время ответа из шага 1, объём собранного брифа (ссылка на бриф этой задачи как доказательство полноты).

- [ ] **Step 3: Прогнать eval replay в двух режимах сидирования**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
cp eval/replay_report.md /private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad/replay_report.before.md
.venv/bin/python -m eval.solve_task_metrics replay --limit 5 --context-seeds lines+signature 2>&1 | tail -20
.venv/bin/python -m eval.solve_task_metrics replay --limit 5 --context-seeds lines 2>&1 | tail -20
```
Записать: агрегаты обоих прогонов.

- [ ] **Step 4: Проверить сохранность ручного хвоста отчёта**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
SCRATCH=/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad
python3 - <<PY
before = open("$SCRATCH/replay_report.before.md").read()
after = open("eval/replay_report.md").read()
marker = "generated:end"
tail_before = before.split(marker, 1)[1] if marker in before else None
tail_after = after.split(marker, 1)[1] if marker in after else None
print("маркер в исходном отчёте:", marker in before)
print("хвост совпадает дословно:", tail_before == tail_after)
print("длина хвоста:", len(tail_after or ""))
PY
```
Записать: наличие маркера, дословность хвоста (ожидание — `True`), длину хвоста.

- [ ] **Step 5: Проверить отказ на отчёте без маркера — до обращения к Voyage**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
SCRATCH=/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad
cp eval/replay_report.md "$SCRATCH/replay_report.keep.md"
python3 -c "
import re
p='eval/replay_report.md'
text=open(p).read()
open(p,'w').write(re.sub(r'<!--\s*generated:end.*?-->','',text,flags=re.S))
"
time .venv/bin/python -m eval.solve_task_metrics replay --limit 5 --context-seeds lines 2>&1 | tail -10
cp "$SCRATCH/replay_report.keep.md" eval/replay_report.md
```
Записать: текст отказа (называет ли он нужную строку-маркер) и время до отказа. Ожидание — отказ за доли секунды, то есть до обращения к Voyage и графу. Восстановление отчёта — последней строкой блока.

- [ ] **Step 6: Запросить санкцию и снять замер исчерпания пула**

Спросить человека: «Создать конкуренцию выше `pg_pool_max_size=4` (параллельные вызовы контекста и синка)?» После согласия:

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
.venv/bin/python - <<'PY'
import concurrent.futures as cf, json, time
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

settings = Settings()
components = build_components(settings)
svc = MCPReviewService(settings, components)

def call(i):
    t0 = time.perf_counter()
    payload = svc.prepare_task_context("mimfort/rag_for_git", "PRI-269", "dev", warm_board=False)
    return i, round(time.perf_counter() - t0, 2), payload["gaps"]

with cf.ThreadPoolExecutor(max_workers=8) as pool:
    for i, elapsed, gaps in pool.map(call, range(8)):
        print(f"call={i} elapsed_s={elapsed} gaps={json.dumps(gaps, ensure_ascii=False)}")
PY
```
Записать: время каждого вызова и `gaps`. Ожидание по критерию 10 — контекст собирается, `PoolTimeout` не выдаётся за недоступность хранилища. Любая запись `cause: storage_unavailable` здесь — дефект, идёт в раздел «Найденные дефекты».

- [ ] **Step 7: Коммит**

```bash
git add eval/pri269_acceptance_report.md
git commit -m "docs(pri-269): замеры на живой инфраструктуре — payload, eval replay, пул"
```

---

### Task 4: Оба хранилища остановлены (шаги 3, 4, 5, 6)

**Files:**
- Modify: `eval/pri269_acceptance_report.md` (разделы «Критерий 1», «Критерий 2», «Критерий 5»)

**Interfaces:**
- Consumes: baseline времени из Task 3.
- Produces: замеры быстрого отказа; подтверждение поведения шага 0a в обоих режимах.

- [ ] **Step 1: Снять watermark и выгрузить пачку задач ДО остановки**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
SCRATCH=/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad
.venv/bin/python - <<PY
import json
import psycopg
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

settings = Settings()
with psycopg.connect(settings.pg_dsn) as conn:
    rows = conn.execute(
        "SELECT repo, ref, sha, updated_at FROM index_meta WHERE ref LIKE 'tasks:%'"
    ).fetchall()
print("watermark_before=", rows)

components = build_components(settings)
svc = MCPReviewService(settings, components)
keys = components.task_store.list_keys("PRI")[:12]
tasks = [t for t in (svc.get_task(k, project="PRI") for k in keys) if t]
print("выгружено задач:", len(tasks))
open("$SCRATCH/pri269_tasks.json", "w").write(json.dumps(tasks, ensure_ascii=False, default=str))
PY
```
Записать: значение watermark до остановки (строки `index_meta` с `ref LIKE 'tasks:%'`) и число выгруженных задач — оно должно быть не меньше 10, иначе взять больший срез `list_keys`.

- [ ] **Step 2: Запросить санкцию и остановить хранилища**

Спросить человека: «Останавливаю ParadeDB и Neo4j (`reviewer stop`)? На время фазы reviewer будет недоступен.» После согласия:

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
.venv/bin/reviewer stop 2>&1 | tail -5
docker compose ps 2>&1 | head -5
```

- [ ] **Step 3: Замерить prepare_task_context на мёртвом сторе (шаг 3, критерий 1)**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
.venv/bin/python - <<'PY'
import json, time
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

settings = Settings()
components = build_components(settings)
svc = MCPReviewService(settings, components)
t0 = time.perf_counter()
payload = svc.prepare_task_context("mimfort/rag_for_git", "PRI-269", "dev", warm_board=False)
print(f"elapsed_s={time.perf_counter() - t0:.2f}")
print("keys=", sorted(payload))
print("gaps=", json.dumps(payload["gaps"], ensure_ascii=False, indent=2))
print("дефолты секций:", {k: payload[k] for k in sorted(payload) if k not in ("gaps", "warnings")})
PY
```
Записать: `elapsed_s` (ожидание — секунды, не десятки минут), `cause` и `remedy` **каждой** записи `gaps` (ожидание — `storage_unavailable` / `reviewer start`), все девять ключей с их дефолтами.

- [ ] **Step 4: Замерить пачку задач на мёртвом сторе (шаг 4, критерий 2)**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
SCRATCH=/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad
.venv/bin/python - <<PY
import json, time
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

tasks = json.load(open("$SCRATCH/pri269_tasks.json"))
print("задач в пачке:", len(tasks))
settings = Settings()
components = build_components(settings)
svc = MCPReviewService(settings, components)
t0 = time.perf_counter()
result = svc.index_tasks_batch(tasks)
print(f"elapsed_s={time.perf_counter() - t0:.2f}")
print("длина результата:", len(result))
print("retry_required у всех:", all(r.get("retry_required") for r in result))
print("ключи первой записи:", sorted(result[0]) if result else None)
PY
```
Записать: `elapsed_s` (ожидание — секунды, одна попытка соединения, не N × 30 с), длина результата равна длине пачки, `retry_required=true` у всех, полный набор ключей результата (включая `prs_linked`).

- [ ] **Step 5: Зафиксировать нулевой расход Voyage**

Расход подтверждается структурно: шаг эмбеддингов под флагом `storage_down` не выполняется. Записать в отчёт: наблюдение из шага 4 (время в секундах при 12 задачах несовместимо с обращением к Voyage при лимите 3 RPM) плюс отсутствие в выводе предупреждений эмбеддера.

- [ ] **Step 6: Пройти шаг 0a скиллом solve-task в режиме normal (шаг 5, критерий 5)**

Запустить `/rag-reviewer:solve-task PRI-269` при остановленных контейнерах. Проверить и записать все три ветки:
- «Поднять сейчас» — предлагается ли (должен, так как эндпоинты loopback), запускает ли `reviewer start`, повторяет ли `prepare_task_context`, продолжает ли;
- «Подниму сам» — ставится ли пауза до подтверждения;
- «Продолжить без контекста» — попадает ли запись в **Constraints / open questions** брифа.

Записать: какие секции скилл назвал потерянными и дословную формулировку предложения.

- [ ] **Step 7: Повторить шаг 0a в режиме full-auto (шаг 6, критерий 5)**

Запустить тот же скилл, выбрав в стартовой панели режим `full-auto`. Записать: задавался ли вопрос (ожидание — нет), какой вариант взят (ожидание — `reviewer start`), появилась ли строка решения в `.superpowers/solve-task/PRI-269.md`.

- [ ] **Step 8: Поднять хранилища обратно и записать результаты**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
.venv/bin/reviewer start 2>&1 | tail -5
git add eval/pri269_acceptance_report.md
git commit -m "docs(pri-269): замеры на остановленных хранилищах и шаг 0a в двух режимах"
```

---

### Task 5: Частичный отказ — мёртв только Neo4j (шаг 12, критерий 9)

**Files:**
- Modify: `eval/pri269_acceptance_report.md` (раздел «Критерий 9»)

**Interfaces:**
- Consumes: baseline полноты payload из Task 3.
- Produces: перечень секций, потерянных сверх `related.linked`.

- [ ] **Step 1: Запросить санкцию и остановить только Neo4j**

Спросить человека: «Останавливаю только Neo4j, ParadeDB оставляю живым?» После согласия:

```bash
SCRATCH=/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad
NEO=$(docker ps --format '{{.Names}}' | grep -i neo4j | grep -v test | head -1)
echo "$NEO" | tee "$SCRATCH/pri269_neo4j_container.txt"
docker stop "$NEO"
docker ps --format '{{.Names}}' | grep -iE 'neo4j|parade'
```
Записать: имя остановленного контейнера и то, что ParadeDB остался в выводе. Если `NEO` пуст — остановиться и сообщить человеку: контейнер не найден по имени, состояние фазы недостижимо.

- [ ] **Step 2: Снять payload при мёртвом Neo4j**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
.venv/bin/python - <<'PY'
import json, time
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

settings = Settings()
components = build_components(settings)
svc = MCPReviewService(settings, components)
t0 = time.perf_counter()
payload = svc.prepare_task_context("mimfort/rag_for_git", "PRI-269", "dev", warm_board=False)
print(f"elapsed_s={time.perf_counter() - t0:.2f}")
for key in ("code", "subsystems", "test_exemplars"):
    value = payload.get(key)
    print(f"{key}: {'СОБРАНА' if value else 'пуста'} ({str(value)[:80]!r})")
related = payload.get("related") or {}
print("related.linked:", str(related.get("linked"))[:80])
print("related.similar:", str(related.get("similar"))[:80])
print("gaps=", json.dumps(payload["gaps"], ensure_ascii=False, indent=2))
PY
```
Записать: собрались ли `code`, `subsystems`, `related.similar` (ожидание по критерию 9 — да, им Neo4j не нужен); сколько секций потеряно сверх `linked`; `cause` каждой записи `gaps`.

- [ ] **Step 3: Классифицировать результат**

Если секции, зависящие только от ParadeDB, отменены общим флагом — это дефект: записать в «Найденные дефекты» с точным перечнем потерянных секций и завести задачу PRI в Task 7.

- [ ] **Step 4: Поднять Neo4j и подтвердить восстановление**

```bash
SCRATCH=/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad
docker start "$(cat "$SCRATCH/pri269_neo4j_container.txt")"
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git && .venv/bin/reviewer check 2>&1 | grep -i neo4j
```

- [ ] **Step 5: Коммит**

```bash
git add eval/pri269_acceptance_report.md
git commit -m "docs(pri-269): замер частичного отказа — мёртвый Neo4j при живом ParadeDB"
```

---

### Task 6: Подменённая конфигурация — не-loopback и неверные креды (шаги 7, 11; критерии 3, 8)

**Files:**
- Modify: `eval/pri269_acceptance_report.md` (разделы «Критерий 3», «Критерий 8»)

**Interfaces:**
- Consumes: отчёт из Task 5.
- Produces: значения `cause`/`remedy` для трёх подменённых конфигураций.

**Методологическое решение:** замеры снимаются в изолированном процессе через `$REVIEWER_ENV_FILE`, а не правкой канонического `~/.config/rag-reviewer/.env`. Канонический файл не трогается, перезапуск `reviewer-mcp` не нужен, действие полностью обратимо. Обоснование корректности: `cause` и `remedy` формируются в `build_task_context`/`storage_health`, то есть до и независимо от MCP-транспорта, поэтому значения тождественны тем, что вернул бы MCP. Ограничение фиксируется в отчёте явно: клиентский текст скилла при не-loopback проверяется отдельно, в шаге 4 этой задачи.

- [ ] **Step 1: Собрать три подменённых env-файла**

```bash
SCRATCH=/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad
BASE=~/.config/rag-reviewer/.env
# не-loopback эндпоинты
sed -E 's#(PG_DSN=postgresql://[^@]+@)[^:/]+#\1db.example.internal#; s#(NEO4J_URI=neo4j://)[^:/]+#\1graph.example.internal#' "$BASE" > "$SCRATCH/env.remote"
# неверный пароль при живых контейнерах
sed -E 's#(PG_DSN=postgresql://[^:]+:)[^@]+@#\1wrongpassword@#' "$BASE" > "$SCRATCH/env.badpass"
# несуществующая база
sed -E 's#(PG_DSN=postgresql://[^/]+/)[^?[:space:]]+#\1nosuchdb#' "$BASE" > "$SCRATCH/env.nodb"
grep -c . "$SCRATCH/env.remote" "$SCRATCH/env.badpass" "$SCRATCH/env.nodb"
```
Проверить глазами, что подмена сработала (`grep -E '^(PG_DSN|NEO4J_URI)' "$SCRATCH/env.remote"` и т. д.), и записать применённые значения **без пароля**.

- [ ] **Step 2: Снять замер при не-loopback эндпоинтах (шаг 7, критерий 3)**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
SCRATCH=/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/da631448-b205-47c9-938a-a743d04ee708/scratchpad
REVIEWER_ENV_FILE="$SCRATCH/env.remote" .venv/bin/python - <<'PY'
import json, time
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

settings = Settings()
print("PG хост:", settings.pg_dsn.split("@")[-1])
components = build_components(settings)
svc = MCPReviewService(settings, components)
t0 = time.perf_counter()
payload = svc.prepare_task_context("mimfort/rag_for_git", "PRI-269", "dev", warm_board=False)
print(f"elapsed_s={time.perf_counter() - t0:.2f}")
print("gaps=", json.dumps(payload["gaps"], ensure_ascii=False, indent=2))
PY
```
**Первым делом сверить в выводе, что подменённый хост действительно подхвачен** — иначе замер зелёный по построению. Записать: `cause` и `remedy` каждой записи (ожидание — `remedy: null`).

- [ ] **Step 3: Снять замеры при неверном пароле и несуществующей БД (шаг 11, критерий 8)**

Повторить блок шага 2 дважды, подставив `env.badpass` и `env.nodb`. Для каждого записать: `cause`, `remedy` и **дословный текст `reason`**, который увидит пользователь.

Оценить по критерию 8: указывает ли текст на настоящую причину (креды / отсутствующая БД) или сводится к совету поднять контейнеры. Если второе — дефект в «Найденные дефекты» и задача PRI в Task 7.

- [ ] **Step 4: Проверить клиентское поведение шага 0a при `remedy: null`**

По полученному payload (`remedy: null`) сверить с текстом скилла: `plugin/skills/solve-task/references/preflight.md` — вариант «Поднять сейчас» показывается только при наличии `remedy`, иначе скилл прямо говорит, что `reviewer start` неприменим.

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
grep -n 'remedy' plugin/skills/solve-task/references/preflight.md | head -10
```
Записать: цитату инструкции и вывод о соответствии.

- [ ] **Step 5: Убедиться, что канонический env не тронут**

```bash
ls -la ~/.config/rag-reviewer/.env
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git && .venv/bin/reviewer check 2>&1 | tail -5
```
Записать: время модификации файла (должно быть прежним) и результат `reviewer check`.

- [ ] **Step 6: Коммит**

```bash
git add eval/pri269_acceptance_report.md
git commit -m "docs(pri-269): замеры не-loopback эндпоинтов и неверных кредов"
```

---

### Task 7: Верификация возврата, заведение дефектов, итоговый отчёт (критерий 11)

**Files:**
- Modify: `eval/pri269_acceptance_report.md` (разделы «Найденные дефекты», «Итог»)

**Interfaces:**
- Consumes: все замеры из Task 1–6.
- Produces: отчёт со ссылками на заведённые задачи PRI; закрытый временный PR.

- [ ] **Step 1: Подтвердить возврат в исправное состояние живым вызовом**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
.venv/bin/python - <<'PY'
import json
from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

settings = Settings()
components = build_components(settings)
svc = MCPReviewService(settings, components)
payload = svc.prepare_task_context("mimfort/rag_for_git", "PRI-269", "dev", warm_board=False)
print("gaps=", json.dumps(payload["gaps"], ensure_ascii=False))
print("ключей:", len(payload))
PY
```
Ожидание — `gaps: []` и девять ключей. Наличие файла `.env` доказательством не считается: подтверждает только этот вызов.

- [ ] **Step 2: Закрыть временный PR**

```bash
cd /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git
gh pr close <номер из Task 2> --comment 'Временный PR приёмки PRI-269, прогон завершён.'
git checkout feat/pri-269-release-0-7-0-acceptance
git push origin --delete chore/pri-269-review-smoke
```

- [ ] **Step 3: Свести найденные дефекты**

Собрать в раздел «Найденные дефекты» все расхождения из Task 2–6. Известный до начала прогона дефект внести обязательно: недоступность Voyage при живых хранилищах даёт `cause: "unknown"` и `remedy: null` для секций `task`/`subsystems`, `code`/`test_exemplars` — «(ничего не найдено)», `related.similar` — «(task search unavailable)», `sync_board` — `failed: 3` с `embedder: APIError: HTTP code 403`; воспроизведено 28.08.2026 на датацентровом IP.

- [ ] **Step 4: Запросить санкцию и завести задачи PRI**

Спросить человека: «Завожу задачи на доске по найденным дефектам (список)?» После согласия использовать `/rag-reviewer:create-task` на каждый дефект, в теле — ссылка на соответствующий раздел `eval/pri269_acceptance_report.md`. Записать ключи заведённых задач обратно в отчёт.

- [ ] **Step 5: Заполнить итог по 11 критериям**

Для каждого критерия — вердикт (пройден / не пройден / пройден с оговоркой) со ссылкой на раздел с замером. Критерий 11 считается выполненным, только когда каждый дефект имеет заведённую задачу и ссылку.

- [ ] **Step 6: Финальный коммит**

```bash
git add eval/pri269_acceptance_report.md
git commit -m "docs(pri-269): итог приёмки релиза 0.7.0 и ссылки на заведённые дефекты"
```

- [ ] **Step 7: Предложить закрытие задачи**

Предложить человеку `/rag-reviewer:finish-task` для PRI-269 (запись в доску — только с санкции), после создания PR приёмки.

---

## Резолв стратегии исполнения

Правило из run-state: `auto` резолвится после написания плана, первое совпадение выигрывает.

Задач — 7 (не более 8). Затронутых файлов — 1 (`eval/pri269_acceptance_report.md`). Но первое правило срабатывает раньше остальных: **есть риск-сигналы** — необратимые внешние действия (публикация ревью в PR, создание и закрытие PR, `git push`, запись задач на доску). Следовательно стратегия — **`subagent`**.
