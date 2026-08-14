# Метрики этапа solve-task

Срез от 2026-08-14T15:21:00.752701+00:00, коммит `9349109c6cadf6aa6dac1593911f4b62063f8e7a`, режим окна замера цены: `sealed`.

## Охват

| Метрика | Значение |
|---|---|
| Брифов в корпусе | 57 |
| С блоком токенов | 28 |
| С ключом задачи | 49 |
| С ground truth (PR-мерж найден) | 43 |
| Отброшено sync-мержей | 11 |

## Цена этапа

- Взвешенный input-эквивалент (основная метрика): **681.5K** (медиана)
- Сырая сумма токенов — **не пропорциональна стоимости**, справочно: 2.81M (медиана); завышает в 4.1×

## Качество ретрива

- core-recall: медиана 61%, среднее 61%, N=34
- Задач без точки измерения (пустой знаменатель ядра): 9 — не считаются нулевым recall
- Медианный размер знаменателя ядра: 4
- Сырой recall (справочно, измеряет выбор знаменателя, не качество ретрива): медиана 18%

## Полная цена задачи «под ключ»

- Измерено задач: 20 (остальные — транскрипт недоступен локально, это не ноль, а отсутствие замера)
- Взвешенный input-эквивалент: медиана 5.97M

## Промахи по категориям

| Категория | Промахов |
|---|---|
| новый файл (не существовал до PR) | 357 |
| tests/ | 236 |
| прочее | 111 |
| plugin/skills/*.md | 37 |
| reviewer/tasks | 30 |
| plugin/ (прочее) | 27 |
| docs/ | 15 |
| reviewer/entrypoints | 9 |
| reviewer/mcp | 7 |
| reviewer/install.py | 4 |
| reviewer/services | 4 |
| reviewer/index | 3 |
| .review.yml/конфиги | 3 |
| reviewer/config | 2 |
| reviewer/app.py | 2 |
| reviewer/policy | 2 |
| reviewer/web | 2 |
| reviewer/launcher | 2 |
| reviewer/gitutil.py | 1 |

## Per-task

| Ключ | Бриф | expected | core | predicted | hit | core-recall |
|---|---|---|---|---|---|---|
| PRI-162 | 2026-06-26-PRI-162-solve-task-include-tests.md | 5 | 0 | 4 | 0 | — |
| PRI-164 | 2026-06-26-PRI-164-solve-task-brief-hygiene.md | 5 | 0 | 6 | 0 | — |
| PRI-196 | 2026-06-28-PRI-196-attachments-sync-board.md | 23 | 10 | 12 | 5 | 50% |
| PRI-176 | 2026-06-29-PRI-176-check-existing-briefs-plans-specs.md | 5 | 0 | 6 | 0 | — |
| PRI-202 | 2026-07-01-PRI-202-configurable-context-limits.md | 32 | 9 | 9 | 5 | 56% |
| PRI-203 | 2026-07-02-PRI-203-reviewer-rag-beyond-brief.md | 13 | 0 | 8 | 0 | — |
| PRI-205 | 2026-07-02-PRI-205-server-side-done-target-discovery.md | 20 | 5 | 9 | 5 | 100% |
| PRI-206 | 2026-07-03-PRI-206-blast-radius-shared-interfaces.md | 8 | 0 | 9 | 0 | — |
| PRI-207 | 2026-07-03-PRI-207-watermark-sync-no-project-backfill.md | 14 | 6 | 7 | 3 | 50% |
| PRI-208 | 2026-07-03-PRI-208-brief-model-choice.md | 7 | 0 | 5 | 0 | — |
| PRI-210 | 2026-07-10-PRI-210-codex-plugin-auto-install.md | 24 | 2 | 3 | 2 | 100% |
| PRI-211 | 2026-07-16-PRI-211-isolate-tests-from-infrastructure.md | 27 | 1 | 5 | 1 | 100% |
| PRI-179 | 2026-07-20-PRI-179-implements-edges-implementations-tool.md | 18 | 4 | 7 | 3 | 75% |
| PRI-212 | 2026-07-20-PRI-212-session-keepalive.md | 13 | 2 | 11 | 2 | 100% |
| PRI-172 | 2026-07-21-PRI-172-solve-task-brief-path-guard.md | 11 | 2 | 2 | 0 | 0% |
| PRI-213 | 2026-07-23-PRI-213-create-task-on-board.md | 28 | 7 | 12 | 5 | 71% |
| PRI-215 | 2026-07-23-PRI-215-extensible-board-providers.md | 101 | 14 | 5 | 5 | 36% |
| PRI-216 | 2026-07-24-PRI-216-test-healthcheck-cpu.md | 5 | 0 | 3 | 0 | — |
| PRI-217 | 2026-07-25-PRI-217-board-registry-8-providers.md | 101 | 4 | 18 | 2 | 50% |
| PRI-218 | 2026-07-25-PRI-218-global-interactive-launcher.md | 32 | 1 | 2 | 1 | 100% |
| PRI-173 | 2026-07-26-PRI-173-subsystem-summary-stale-read-path.md | 11 | 4 | 2 | 2 | 50% |
| PRI-177 | 2026-07-28-PRI-177-brief-spec-traceability.md | 7 | 1 | 4 | 0 | 0% |
| PRI-219 | 2026-07-28-PRI-219-status-json-summaries-field.md | 16 | 4 | 13 | 2 | 50% |
| PRI-220 | 2026-07-28-PRI-220-bilingual-readme-onboarding.md | 8 | 0 | 0 | 0 | — |
| PRI-134 | 2026-07-29-PRI-134-risk-signals-nonpython.md | 17 | 4 | 4 | 2 | 50% |
| PRI-178 | 2026-07-29-PRI-178-reranker-fallback-preserve-graph-items.md | 7 | 1 | 2 | 1 | 100% |
| PRI-221 | 2026-07-30-PRI-221-home-repository-config-layer.md | 34 | 9 | 0 | 0 | 0% |
| PRI-225 | 2026-07-31-PRI-225-task-board-retention-filter.md | 77 | 18 | 10 | 7 | 39% |
| PRI-227 | 2026-07-31-PRI-227-remove-reviewer-skill-name-duplication.md | 38 | 3 | 3 | 0 | 0% |
| PRI-223 | 2026-08-07-PRI-223-redesign-reviewer-init.md | 83 | 25 | 7 | 6 | 24% |
| PRI-222 | 2026-08-09-PRI-222-runtime-web-container-port.md | 13 | 1 | 4 | 1 | 100% |
| PRI-234 | 2026-08-10-PRI-234-vcs-failure-must-not-drop-local-policy-layers.md | 18 | 4 | 9 | 4 | 100% |
| PRI-228 | 2026-08-11-PRI-228-token-budget-embedding-batches.md | 7 | 4 | 5 | 3 | 75% |
| PRI-235 | 2026-08-11-PRI-235-local-committed-policy-layer.md | 15 | 4 | 6 | 4 | 100% |
| PRI-236 | 2026-08-11-PRI-236-acceptance-0-4-5.md | 8 | 2 | 6 | 2 | 100% |
| PRI-237 | 2026-08-11-PRI-237-drop-fingerprint-echo-from-file-job.md | 7 | 1 | 4 | 0 | 0% |
| PRI-238 | 2026-08-11-PRI-238-task-link-status.md | 11 | 3 | 4 | 2 | 67% |
| PRI-239 | 2026-08-11-PRI-239-report-bug-channel.md | 36 | 6 | 8 | 4 | 67% |
| PRI-241 | 2026-08-12-PRI-241-parametrize-storage-publish-ports.md | 12 | 2 | 8 | 2 | 100% |
| PRI-242 | 2026-08-12-PRI-242-reviewer-start-stop-infra-cli.md | 13 | 2 | 7 | 2 | 100% |
| PRI-243 | 2026-08-12-PRI-243-solve-task-interaction-mode-and-execution-strategy.md | 15 | 2 | 7 | 0 | 0% |
| PRI-245 | 2026-08-13-PRI-245-cheaper-summarize-subsystems.md | 39 | 9 | 10 | 5 | 56% |
| PRI-246 | 2026-08-14-PRI-246-solve-task-cost-quality-spike.md | 3 | 0 | 7 | 0 | — |
