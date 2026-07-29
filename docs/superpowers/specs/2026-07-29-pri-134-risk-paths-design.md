# PRI-134 — риск-сигналы не-Python изменений

## Цель

Глобально установленный `rag-reviewer` должен замечать узкий класс опасных изменений,
которые сейчас молча выпадают из Python-only review pipeline: миграции БД,
CI/deploy/infra, dependency/lock и credential-like файлы. Решение не расширяет
Python-индекс, не отправляет обычные конфиги на дополнительный анализ и не превращает
эвристический path-сигнал в finding.

## Выбранный подход

`ReviewService.prepare()` строит отдельный bounded список `risk_paths` из полного
`ChangedFile[]`. Чистый классификатор смотрит только на нормализованный путь и статус,
возвращает стабильные причины риска и детерминированно ранжирует кандидатов. Обычные
`.yaml`, `.json` и `.toml` не считаются рискованными без контекстного якоря в пути или
имени файла.

`risk_paths` не входят в `units`, `changed_paths`, overlay, chunker или code graph.
Для выбранных non-removed путей сервис один раз загружает head-source исключительно
для существующего grounding/publish tail. Embeddings и структурный diff для них не
строятся.

MCP payload отдаёт каждый выбранный risk item вместе с `path`, `status`, `reasons`,
`patch`, `commentable_right` и `commentable_left`. Отдельный `risk_skipped_paths`
перечисляет кандидатов сверх лимита. Persisted session хранит типизированные сигналы;
старый payload без новых полей восстанавливается с пустыми списками.

## Классификатор

Классификатор живёт в отдельном модуле сервисного слоя и не зависит от VCS, Settings,
MCP или LLM. Он исключает `.py`, нормализует `/`, сравнивает path segments и basenames
без учёта регистра и присваивает одну или несколько причин:

- `migration`: сегменты `migrations`, `alembic/versions`, `db/migrate` или SQL-файл;
- `ci_deploy_infra`: CI workflows, Docker/Compose, Jenkins, Helm/Kubernetes,
  Terraform и явно infra/deploy-скоупленные manifests;
- `dependency`: известные manifests и lockfiles популярных экосистем;
- `credential_like`: `.env*`, `.npmrc`, `.pypirc`, `.netrc` и явно названные
  secrets/credentials manifests.

Приоритет: credential-like → migration → CI/deploy/infra → dependency, затем
важность diff (hunks/длина) и лексикографический path. Один путь появляется один раз
со всеми причинами. Жёсткий лимит первой версии — 10 путей на PR: этого достаточно
для bounded LLM-контекста, а отдельная настройка до появления реального кейса не
нужна.

Удалённые risk-файлы классифицируются: удаление migration/deploy/dependency manifest
само может быть опасным. При недоступном patch сигнал сохраняется, а skill сообщает,
что доказательство проверить нельзя, и не создаёт finding.

## Skill flow

`review-pr` запускает один дополнительный whole-diff subagent только когда
`risk_paths` непуст. Отдельный prompt:

- рассматривает только переданные diff и намерение PR;
- проверяет семантические риски миграций, deployment/CI/infra, согласованность
  dependency manifest ↔ lockfile и случайную публикацию credentials;
- использует обычные категории `correctness`/`security`;
- требует точный `code_quote` и commentable coordinate;
- не считает path/reason доказательством и не создаёт finding без конкретной проблемы;
- не повторяет и не цитирует секрет в message;
- submit-ит findings через существующий `submit_findings`.

`risk_skipped_paths` попадает в итоговую сводку рядом с обычными skipped/failed
файлами, чтобы ограничение анализа было видимым.

## Совместимость и ошибки

- Python-only поведение остаётся прежним, включая порядок/лимит units и overlay.
- Новый persisted payload обратно совместим при чтении старых сессий.
- Ошибка чтения risk head-source не валит prepare: item остаётся с patch, но inline
  grounding может деградировать в summary.
- Пустой или бинарный patch не анализируется как доказательство.
- Risk dimension fail-open, как остальные analyze/dimension subagents; его сбой
  отражается в summary и не блокирует публикацию других findings.

## Тестирование

1. Чистые unit-тесты классификатора: положительные семейства, обычные config-файлы,
   `.py`, case/path normalization, multi-reason, removed, deterministic order/cap.
2. `ReviewService.prepare`: risk candidates не входят в overlay/units/changed_paths,
   выбранный head-source доступен для grounding, overflow отражён отдельно.
3. MCP payload: patch/reasons/status/commentable coordinates и skipped paths.
4. Session serde: новые поля round-trip; старый payload без них восстанавливается.
5. Skill guards: `review-pr` содержит условный risk dimension и prompt закрепляет
   evidence/noise/secret-redaction правила.
6. Регрессия: существующие service/MCP/session/skill тесты и Ruff.

## Не входит в scope

- Парсеры SQL/YAML/Terraform и статический secret scanner.
- Индексация или graph nodes для non-Python файлов.
- Пользовательские glob-паттерны и настраиваемый cap до появления подтверждённой
  необходимости.
- Изменение category schema, publish gate или verify pipeline.
