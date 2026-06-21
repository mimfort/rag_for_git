# Remove `web` Compose Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Удалить docker-compose-сервис `web`, чтобы `docker compose up` на свежем клоне не тянул тяжёлую сборку фронта, и синхронизировать документацию.

**Architecture:** Чисто инфраструктурно-документационная правка. Вырезается блок `services.web` из `docker-compose.yml`; упоминания «compose поднимает web» удаляются из `CLAUDE.md`, `README.md`, `.env.example`. Python-код, фронтенд-исходники, `web/Dockerfile`, `[web]`-extra и команда `reviewer serve` НЕ трогаются — админка остаётся доступной на хосте.

**Tech Stack:** Docker Compose (YAML), Markdown-доки.

## Global Constraints

- Язык доков/комментариев — **русский** (кроме `README.md`, где целевые строки на английском — сохранять английский в них).
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- НЕ удалять и НЕ менять: `reviewer/web/`, `web/Dockerfile`, `web/frontend/`, `[web]`-extra в `pyproject.toml`, команду `reviewer serve`, строки про хостовый `reviewer serve` в доках.
- Номера строк ниже — ориентир; матчить по точному тексту (правки сдвигают последующие строки).
- Работаем на ветке `chore/remove-web-compose-service` (уже создана, спека закоммичена).

---

### Task 1: Удалить сервис `web` из `docker-compose.yml`

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: ничего.
- Produces: `docker-compose.yml` с `services` = `paradedb` + `neo4j` (без `web`), `volumes` без изменений.

- [ ] **Step 1: Удалить блок `services.web`**

Удалить из `docker-compose.yml` весь блок (вместе с комментарием), точный текущий текст:

```yaml
  web:
    # Веб-админка наблюдаемости: фронт собирается внутри образа, бэкенд (FastAPI)
    # отдаёт SPA + JSON-API. Читает ту же БД, что пишет `reviewer review` на хосте.
    build:
      context: .
      dockerfile: web/Dockerfile
    environment:
      # внутри compose-сети Postgres доступен как paradedb:5432 (не host:5433)
      PG_DSN: postgresql://reviewer:reviewer@paradedb:5432/reviewer
    ports: ["127.0.0.1:8000:8000"]   # loopback-only, как остальной стек
    depends_on: ["paradedb"]
```

После удаления `volumes:`-секция (`paradedb_data` / `neo4j_data`) остаётся как есть — её НЕ трогать. Файл должен заканчиваться блоком `neo4j`, затем `volumes:`.

- [ ] **Step 2: Проверить, что compose валиден и без `web`**

Run: `docker compose config --services`
Expected: ровно две строки — `paradedb` и `neo4j` (никакого `web`). Команда завершается с кодом 0 (YAML валиден).

Если `docker compose` недоступен в окружении — fallback-проверка валидности YAML:
Run: `python -c "import yaml,sys; d=yaml.safe_load(open('docker-compose.yml')); assert set(d['services'])=={'paradedb','neo4j'}, d['services']; print('OK', list(d['services']))"`
Expected: `OK ['paradedb', 'neo4j']`

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "chore(compose): удалить сервис web из docker-compose"
```

---

### Task 2: Синхронизировать документацию

Убрать из доков утверждения, что `docker compose up` поднимает веб-админку. Строки про хостовый `reviewer serve` оставить.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: результат Task 1 (web больше не в compose).
- Produces: доки без упоминаний docker-сервиса web; grep-проверка пустая.

- [ ] **Step 1: Правки в `CLAUDE.md`**

Правка 1 — строка инфраструктуры (убрать web-админку из перечня сервисов compose):

old:
```
# Инфраструктура: ParadeDB (5433) + Neo4j (7687) + web-админка наблюдаемости (:8000, сервис web)
```
new:
```
# Инфраструктура: ParadeDB (5433) + Neo4j (7687)
```

Правка 2 — удалить строку про «Проще: docker compose поднимает админку». Удалить целиком строку:
```
# Проще: `docker compose up -d` поднимает админку как сервис web (:8000) — фронт собирается в образе.
```
Соседняя строка `# На хосте (для разработки фронта): pip install -e ".[web]" ... reviewer serve` — **остаётся**.

Правка 3 — инвариант наблюдаемости (убрать «или сервис web в docker-compose»):

old:
```
- **Наблюдаемость (`reviewer/web/`)**: каждый `publish_review` пишет в Postgres итоги прогона (`review_runs`/`review_findings`, гейт `REVIEW_HISTORY`) — fail-soft. Веб-админка (FastAPI `reviewer serve` или сервис `web` в docker-compose) читает **ту же** БД.
```
new:
```
- **Наблюдаемость (`reviewer/web/`)**: каждый `publish_review` пишет в Postgres итоги прогона (`review_runs`/`review_findings`, гейт `REVIEW_HISTORY`) — fail-soft. Веб-админка (FastAPI `reviewer serve`) читает **ту же** БД.
```

- [ ] **Step 2: Правки в `README.md`**

Правка 1 — строка `Stores (Docker)` в ASCII-диаграмме (убрать web admin):

old:
```
  Stores (Docker):  Postgres/ParadeDB (:5433)  ·  Neo4j (:7687)  ·  web admin (:8000)
```
new:
```
  Stores (Docker):  Postgres/ParadeDB (:5433)  ·  Neo4j (:7687)
```

Правка 2 — quick-start «Infrastructure» комментарий:

old:
```
docker compose up -d          # Postgres/ParadeDB (:5433) + Neo4j (:7687) + web admin (:8000)
```
new:
```
docker compose up -d          # Postgres/ParadeDB (:5433) + Neo4j (:7687)
```

Правка 3 — описание `WEB_ADMIN_USER` в таблице (убрать `/ the web service`):

old:
```
| `WEB_ADMIN_USER` | `""` | Basic-auth user for `reviewer serve` / the `web` service; empty = no auth. |
```
new:
```
| `WEB_ADMIN_USER` | `""` | Basic-auth user for `reviewer serve`; empty = no auth. |
```

Правка 4 — блок запуска web-админки (раздел наблюдаемости). Убрать docker-вариант, оставить хостовый:

old:
```bash
# Via Docker (no manual steps) — the `web` service builds the frontend and serves FastAPI:
docker compose up -d           # Postgres + Neo4j + web admin → http://127.0.0.1:8000

# On the host (for frontend dev):
pip install -e ".[web]"
(cd web/frontend && npm install && npm run build)
reviewer serve                 # http://127.0.0.1:8000 (options: --host / --port)
```
new:
```bash
# On the host — build the frontend, then serve the SPA + FastAPI:
pip install -e ".[web]"
(cd web/frontend && npm install && npm run build)
reviewer serve                 # http://127.0.0.1:8000 (options: --host / --port)
```

- [ ] **Step 3: Правка в `.env.example`**

old:
```
WEB_ADMIN_USER=                    # basic-auth логин для `reviewer serve` / docker-сервиса web
```
new:
```
WEB_ADMIN_USER=                    # basic-auth логин для `reviewer serve`
```

- [ ] **Step 4: Проверить, что битых упоминаний не осталось**

Run:
```bash
grep -rniE "сервис web|web admin \(:8000\)|docker-сервиса web|поднимает админку как сервис web|web-админка наблюдаемости \(:8000, сервис web\)" CLAUDE.md README.md .env.example
```
Expected: пустой вывод (grep exit code 1 — ничего не найдено).

- [ ] **Step 5: Sanity — Python-тесты зелёные (код не менялся)**

Run: `.venv/bin/pytest -q`
Expected: PASS (как до правок; правки не касаются Python).

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md .env.example
git commit -m "docs: убрать упоминания docker-сервиса web после его удаления"
```

---

## Self-Review

**1. Spec coverage:**
- Спека §1 (удалить блок `services.web`) → Task 1. ✓
- Спека §2 (синхронизация доков: CLAUDE.md, README.md, .env.example) → Task 2, шаги 1–3. ✓
- Спека §3 (что НЕ трогаем) → зафиксировано в Global Constraints + явно «остаётся» по строкам. ✓
- Спека «Проверка» (docker compose config без web / grep пуст / pytest зелёный) → Task 1 Step 2, Task 2 Step 4, Task 2 Step 5. ✓

**2. Placeholder scan:** Все правки даны точными old→new блоками; плейсхолдеров нет. ✓

**3. Type consistency:** Не применимо (нет кода/сигнатур). Имена файлов и команды согласованы между задачами. ✓
