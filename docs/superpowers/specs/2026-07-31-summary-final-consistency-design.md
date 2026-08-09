# Final consistency design for subsystem summaries

## Goal

Сделать layout generation, prune/finalization, embedding backfill и межкластерный
reuse устойчивыми к policy- и write-races без доверия к клиентскому состоянию.

## Layout identity

Canonical `layout_token` — SHA-256 детерминированного JSON с
`default_depth` и нормализованными override-парами, отсортированными по prefix.
Токен вычисляется server-side из effective policy и возвращается `list`.

`subsystem_summary_state` хранит nullable `completed_layout`; idempotent schema
migration добавляет колонку к существующим таблицам. Legacy row с `NULL` считается
незавершённым. Fragment provenance всегда получает server-owned точную метку:

```json
{
  "generation": "summary-fragment-v1",
  "layout_token": "<sha256>",
  "depth": 2
}
```

Полнота кластера требует точного same-cluster покрытия всех текущих
path/fingerprint этой меткой. Глобальный rebuild определяется несовпадением
`completed_layout`, а не только depth.

## Verified prune

Успешный uncapped `list` передаёт в prune свой `layout_token` и
`{cluster_key: source_hash}`. Service повторно выводит current layout/clusters и
отклоняет legacy/mismatch до store.

Store под advisory lock и до любого DELETE/UPDATE проверяет:

- каждый expected cluster summary существует с exact source hash;
- каждый current path имеет exact same-cluster fingerprint и generation stamp;
- layout token совпадает с generation stamp.

При любой неполноте транзакция ничего не удаляет и не продвигает state; ответ
имеет `completed=false`, `race=true`, ненулевой `deferred`. Только полная проверка
атомарно удаляет orphans и сохраняет `completed_depth` + `completed_layout`.

## Embeddings and ambiguous moves

Backfill snapshot содержит `source_hash`, `title`, `summary`; запись использует
exact CAS по всем трём полям и считает только успешные UPDATE.

Pure delta хранит все exact cross-cluster candidates. Один кандидат переносится;
два и более делают path pending `changed`, после чего атомарный bundle commit
записывает новый fragment и удаляет cross-cluster duplicates.

## Verification

Unit regressions покрывают layout override, capped convergence, prune races,
premature/incomplete prune, exact embedding CAS и ambiguous move. Реальный
SummaryStore integration подтверждает schema migration и транзакционные
инварианты. Skill guards фиксируют новый prune payload и partial/raced semantics.
