# reviewer init — заполнение новых полей + связка с configure-review

**Дата:** 2026-06-24
**Статус:** дизайн согласован
**Связанные:** PRI-122 (reviewer init wizard), PRI-168 (configure-review skill), PRI-133 (GitLab VCS)

## 1. Проблема

`reviewer init` (PRI-122) создавал wizard для первоначальной настройки `.env`. С тех пор в `Settings` и `.env.example` добавились новые поля — GitLab VCS (PRI-133), YouTrack-доска, веб-админка, `yougile_api_base` — но wizard их не запрашивает. Пользователь должен руками дописывать их в `.env`.

Скилл `configure-review` (PRI-168) настраивает `.review.yml` (per-repo), но не проверяет, заполнены ли нужные поля в глобальном `.env` (GitLab-токен, ключи доски).

## 2. Цель

1. Расширить `WIZARD_GROUPS` в `reviewer/install.py` — добавить все поля из `.env.example`, которых нет в wizard
2. Синхронизировать `ENV_TEMPLATE` с `.env.example`
3. В `configure-review` SKILL.md добавить проверку `.env` перед основной работой — предлагать `reviewer init` если поля отсутствуют

## 3. Новые группы wizard

Текущие 4 группы (15 полей) → 6 групп (22 поля).

### 3.1 Группа «GitLab VCS» (optional)

| Ключ | secret | default | prompt |
|------|--------|---------|--------|
| `GITLAB_TOKEN` | да | `""` | GITLAB_TOKEN (PAT: api scope) |
| `GITLAB_URL` | нет | `https://gitlab.com` | GITLAB_URL (self-hosted base URL) |
| `VCS_PROVIDER` | нет | `github` | VCS_PROVIDER (фолбэк: github) |

Размещается после «Мульти-репо / ветки», перед «Доска задач».

### 3.2 Группа «Веб-админка» (optional)

| Ключ | secret | default | prompt |
|------|--------|---------|--------|
| `WEB_ADMIN_USER` | нет | `""` | WEB_ADMIN_USER (basic-auth логин) |
| `WEB_ADMIN_PASSWORD` | да | `""` | WEB_ADMIN_PASSWORD (basic-auth пароль) |

Размещается последней (после «Доска задач»).

### 3.3 Дополнение группы «Доска задач»

Добавить одно поле:

| Ключ | secret | default | prompt |
|------|--------|---------|--------|
| `YOUGILE_API_BASE` | нет | `""` | YOUGILE_API_BASE (base URL, пусто=дефолт) |

Разместить после `YOUGILE_API_KEY`.

## 4. ENV_TEMPLATE

Заменить текущий `ENV_TEMPLATE` (install.py:42-64, ~22 строки) на полную версию, соответствующую `.env.example` по секциям и комментариям (~80 строк). Все секции с заголовками `# --- ... ---`, пустые значения для ключей. Используется как встроенная документация; реальные значения заполняет wizard через `render_env`.

## 5. configure-review: шаг 1.5 — проверка .env

Добавляется в pipeline скилла (`plugin/skills/configure-review/SKILL.md`) между Preflight (шаг 1) и Scan (шаг 2).

### Логика

```
1.5. Check .env completeness (offer reviewer init if needed).

  Resolve .env path:
    - $REVIEWER_ENV_FILE env var
    - $XDG_CONFIG_HOME/rag-reviewer/.env  (default: ~/.config/rag-reviewer/.env)
    - ./.env  (cwd fallback, dev convenience)

  Read and parse KEY=VALUE lines (skip comments/blank).

  If file doesn't exist → tell user (Russian):
    ".env не найден по пути <path>. Запустить `reviewer init`?"

  If file exists, check critical groups:
    - GitLab VCS: GITLAB_TOKEN (empty → warning)
    - Доска задач: YOUGILE_API_KEY / YOUTRACK_TOKEN (both empty → warning)

  If any missing → tell user (Russian): "В .env не хватает полей: <list>.
    Запустить `reviewer init` чтобы дополнить?"

  User can decline → skill continues normal pipeline.
  This check is read-only (parse KEY=VALUE), no reviewer MCP/DB needed.
```

### Ограничения

- Проверка — мягкая (best-effort). Если пользователь отказывается — скилл продолжает.
- Скилл **не запускает** `reviewer init` автоматически — только предлагает. Пользователь должен явно согласиться.
- Скилл не проверяет ВСЕ поля .env — только критичные для его работы (без GitLab-токена не будет ревью GitLab MR, без ключей доски — не будет связи PR↔task).

## 6. Инварианты

- **Не ломаем обратную совместимость:** существующие `EnvGroup`, `EnvField`, `read_env`, `render_env`, `prompt_groups` не меняют сигнатуры. `_GROUP_HEADERS` дополняется, но формат тот же.
- **Порядок групп:** Обязательные → Хранилища → Мульти-репо/ветки → GitLab VCS → Доска задач → Веб-админка. Соответствует `.env.example`.
- **configure-review остаётся standalone:** проверка `.env` — чистый парсинг файла, не требует reviewer MCP.
- **CI-режим (`--yes`):** новые опциональные группы пропускаются (берутся текущие значения или дефолты).

## 7. Тесты

Файл: `tests/test_install_wizard.py` (дополнить существующие)

- `render_env` с новыми группами: поля GitLab и веб-админки на месте, заголовки корректны
- `prompt_groups` с `yes=True`: новые поля получают дефолты, опциональные группы пропускаются
- `reviewer init --yes` через `click.testing.CliRunner`: создаёт файл со всеми 6 секциями

## 8. Затрагиваемые файлы

| Файл | Изменение |
|------|-----------|
| `reviewer/install.py` | WIZARD_GROUPS (+2 группы, +1 поле), _GROUP_HEADERS (+2), ENV_TEMPLATE (полная замена) |
| `plugin/skills/configure-review/SKILL.md` | Шаг 1.5 между Preflight и Scan |
| `tests/test_install_wizard.py` | Тесты на новые группы и render_env |
