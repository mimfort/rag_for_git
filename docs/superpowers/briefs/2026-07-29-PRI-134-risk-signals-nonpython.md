# Brief — PRI-134 Детерминированные риск-сигналы для не-Python изменений

https://ru.yougile.com/team/686c049c8af8/#PRI-134

## Task

- Store-first task `ID-134` (alias `PRI-134`) после sync_board: старое описание предлагает анализировать выбранные не-`.py` файлы без embeddings и ловить миграции/секреты; критерии в store пусты.
- Актуальная полезная цель: не превращать не-Python файлы в review units, а детерминированно вынести в payload только рискованные пути/статусы и обязать global `review-pr` skill проверить их diff целиком.
- Обычные конфиги не должны создавать finding или дополнительный LLM-анализ; сигнал — лишь routing/context для отдельной whole-diff проверки.

## Related work

- PRI-158 — уже доставил structural diff только для Python; не расширять tree-sitter/embeddings на конфиги.
- PRI-115 — локальное ревью может позднее потребить тот же payload, поэтому классификатор должен жить в сервисном слое, а не только в prompt skill.

(dropped 5: PRI-206/155 относятся к blast radius Python-символов, PRI-169 к init, PRI-119 к walkthrough, PRI-141 к freshness — не задают механизм сигналов.)

## Subsystems

- reviewer/services — prepare уже собирает diff, policy и serializable PR-session payload.
- reviewer/policy — источник repo-configured ограничений; не смешивать с секретами или credentials.
- tests/entrypoints — существующий стиль проверяет отбор changed files и payload на моках.

## Relevant code

- `reviewer/services/review_service.py:72` — `_select_changed_files` намеренно оставляет только не-removed `.py`; сохранить это для overlay, `ReviewUnit` и per-file agents.
- `reviewer/services/review_service.py:209` — `prepare()` уже получает полный `files` до Python-фильтра и может классифицировать risk paths без чтения/embedding содержимого.
- `reviewer/services/review_service.py:271` — выбранные Python файлы формируют `changed`; новый список не должен попасть в `build_overlay`.
- `reviewer/services/review_service.py:340` — `changed_status` уже содержит статусы всех файлов; risk payload должен сохранять тот же status и path.
- `reviewer/mcp/session_serde.py:18` — новое поле `PreparedReview` требует явного добавления в `to_payload` и восстановления в `from_payload`.
- `reviewer/entrypoints/mcp_server.py:27` — `prepare_review` является единственным contract boundary для global plugin.
- `plugin/skills/review-pr/SKILL.md:29` — skill уже делает whole-diff dimensions параллельно с Python per-file agents; risk-only non-Python diff естественно направить в один bounded safety subagent, без inline-комментариев.

(dropped 0: codebase retrieval returned no snippets, so the listed citations are verified against local main checkout rather than reviewer search output.)

## Test exemplars

- `tests/services/test_review_service.py:75` — `_changed()` and mock VCS provide the existing fixture pattern for paths, statuses and patches.
- `tests/services/test_review_service.py:176` — payload/PreparedReview assertions establish the expected Python-only unit invariant.
- `tests/index/test_freshness.py:134` — regression test already asserts non-`.py` inputs are not indexed or cleaned; preserve it and add risk-path cases separately.
- `tests/mcp/test_session_serde.py:76` — schema-parity test requires every new `PreparedReview` field to survive session serialization.

(dropped 0: test retrieval also returned no snippets; citations are local-main verification.)

## Constraints / open questions

- [task_staleness] Original wording says “selected non-.py units”; that conflicts with the current Python-only chunker/overlay/graph contract and would add cost/noise without a parser or grounding strategy.
- [scope] Classify by conservative path/name/status heuristics (e.g. migrations, CI/deploy/infra manifests, dependency/lock changes, `.env`/credential-like files); classifier не читает содержимое, а skill получает только bounded diff выбранных путей по обычной trust-модели review pipeline.
- [signal_semantics] A signal is not a finding. The skill must inspect `get_changed_file_diff` and submit a finding only when it can ground a concrete risk; ordinary `.yaml/.json/.toml` changes remain quiet.
- [boundedness] Cap and report risk paths; when above cap, provide a deterministic summary and say which paths were not inspected.
- [compatibility] Keep `units`, `changed_paths`, `skipped_paths`, overlay and `changed_node_ids` Python-only; add a distinct, backward-compatible payload field and round-trip it through persisted MCP sessions.
- [retrieval_gap] Despite preflight reporting `main` drift 0 and warm summaries, both prescribed `search_codebase` calls and graph definitions returned “nothing found”; no search-snippet citations or graph expansion were available. Validate against local `main` before implementation/reindex if this mismatch persists.
- [existing_artifacts] No PRI-134 brief/spec/plan artifact was found before this write. The working tree already contains unrelated untracked user artifacts; they were left untouched.

Собран на: mid tier (gpt-5.6-terra), режим: subagent
