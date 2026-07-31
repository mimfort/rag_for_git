# Brief — PRI-226 Сделать subsystem summaries инкрементальными на уровне файлов
https://ru.yougile.com/team/686c049c8af8/#PRI-226

## Task

- Данные задачи получены из reviewer store после sync: ID-280 (alias PRI-226), статус «Бэклог».
- Хранить file-summary fragments по `(repo, branch, cluster_key, path)` с fingerprint, текстом, provenance и временем обновления.
- Из current members строить path→fingerprint delta (`changed`, `added`, `removed`); суммаризировать только changed/added, удалять исчезнувшее и переносить fragments между кластерами.
- Из fragments и дельт пересобирать итоговую cluster-summary, не меняя публичный cluster-level API; сохранять только при optimistic-проверке aggregate hash.
- Пересчитывать embedding лишь после успешной сборки; добавить bootstrap, full rebuild при depth change, метрики created/reused/removed/deferred и pipeline-тесты.
- Критерии приёмки находятся в description, отдельный `criteria=[]` в store.

## Related work

- PRI-165 — повторно использовать skeleton_hash как дефолтную fingerprint-семантику, cap/deferred и проверку консистентности при list→persist (PR #53).
- PRI-166 — использовать единый server-side depth resolution и очистку orphaned cluster summaries при смене depth (PR #54).
- PRI-159 — сохранять совместимый cluster-level путь GraphRAG summaries и его consumers.
- PRI-167 — сохранять правило: embedding обновляется только когда успешно изменился итоговый source hash.
(dropped 16: остальные найденные задачи не задают механизм fragments, freshness или lifecycle summary.)

## Subsystems

- reviewer/index — Postgres/pgvector storage; существующий SummaryStore хранит cluster-level summaries и embeddings.
- reviewer/graph — кластеризация members и детерминированный aggregate source_hash от `(node_id, skeleton_hash)`.
- tests/graph — unit-инварианты кластеризации и skeleton-based freshness.
- tests/index — integration-проверки SummaryStore и схемы БД.

## Relevant code

- reviewer/index/summary_store.py:43 — `upsert_summary` делает cluster-level upsert и COALESCE embedding; расширить хранилищем fragment-ов и атомарным persist/cleanup.
- reviewer/index/summary_store.py:67 — `delete_summaries_except` уже чистит cluster-level orphans; согласовать с orphaned/moved file fragments.
- reviewer/mcp/service.py:698 — `index_subsystem_summary` повторно выводит members и сравнивает aggregate hash; заменить текущий fail-soft пустой members на optimistic reject/stale при гонке.
- reviewer/mcp/service.py:921 — `list_subsystem_clusters` строит current clusters, stale/orphans и deferred; здесь нужен file-level delta контракт для skill.
- reviewer/mcp/service.py:1044 — `_current_subsystem_hashes` определяет freshness read-path; сохранить его совместимость с новым aggregate state.
- reviewer/graph/summaries.py:73 — `build_clusters` выдаёт files, member ids и source_hash; использовать его current member map без смены skeleton_hash семантики.
- reviewer/index/store.py:236 — `list_base_members` является источником current path/symbol/skeleton data для дельт.
- reviewer/entrypoints/mcp_server.py:281 — MCP registration `list_subsystem_clusters`; публичный shape требуется сохранить совместимым.
(dropped 22: низкорелевантные retrieval/installation/agent результаты и косвенные graph neighbours не являются точками изменения.)

## Test exemplars

- tests/mcp/test_subsystem_summaries.py:26 — MagicMock fixture проверяет stale-кластер от base members; расширить сценариями changed/added/removed/moved и отсутствием чтения unchanged file.
- tests/mcp/test_subsystem_summaries.py:66 — round-trip list/index/get фиксирует server-derived members и совместимость read API.
- tests/mcp/test_subsystem_summaries.py:103 — cap/deferred ordering; сохранить и дополнить fragment metrics/deferred work.
- tests/mcp/test_subsystem_summaries.py:186 — depth/orphans regression; использовать для full rebuild и cleanup при смене depth.
(dropped 27: оставшиеся найденные тесты покрывают read freshness, embeddings и не дают отдельного шаблона pipeline fragments.)

## Constraints / open questions

- Fingerprint по умолчанию остаётся текущим `skeleton_hash`; переход на full-content invalidation — отдельное решение с оценкой стоимости.
- Нужны явные транзакционные/optimistic semantics: гонка list→persist не должна маркировать новый состав fresh.
- Bootstrap должен сохранить доступные существующие cluster summaries; требуется определить, когда fragment provenance считается достаточным для первого incremental run.
- Task context не содержит linked tasks или touched code; related work взята из semantic search и PR #53/#54.

Собран на: mid tier (gpt-5.6-terra), режим: subagent
