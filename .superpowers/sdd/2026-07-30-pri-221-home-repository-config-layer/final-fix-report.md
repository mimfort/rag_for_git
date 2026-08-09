# PRI-221 final fix report

## Revision

- Starting SHA: `89e839b468ab6c28257489479bb323ed55b7eb98`.
- Final fix commit: this report's commit, `fix: harden layered config integration`
  (resolve with `git rev-parse HEAD` on `feature/pri-221-home-config`).

## Scope and files

- `reviewer/config/layers.py`: known-key home validation, type-sensitive
  semantic comparison, immutable migration snapshot, pre-publication effective
  validation, dotted paths, and hardened destination traversal/publication.
- `reviewer/entrypoints/cli.py`: migration receives the already-created
  `Settings` instance for canonical effective-policy validation.
- `reviewer/mcp/service.py`: policy fail-soft logs no longer serialize raw
  exception tracebacks.
- `tests/conftest.py`: every test gets an empty per-test
  `XDG_CONFIG_HOME`; integration policy setup explicitly depends on it.
- Focused regressions and removal of local duplicate home fixtures:
  `tests/config/test_layers.py`,
  `tests/entrypoints/test_config_commands.py`,
  `tests/entrypoints/test_index_ignore.py`,
  `tests/mcp/test_context_limits_wiring.py`,
  `tests/mcp/test_subsystem_summaries.py`,
  `tests/mcp/test_summary_depth_overrides.py`,
  `tests/services/test_prepare_ignore.py`, and
  `tests/services/test_review_service.py`.
- `.superpowers/sdd/2026-07-30-pri-221-home-repository-config-layer/`
  `final-fix-report.md`: RED/GREEN, verification, review, and open concerns.

## RED evidence

Each production group was preceded by a focused failing run:

1. Home semantic validation/redaction: five failures showed an invalid known
   value merging despite shadowing, strict mode not raising, MCP aborting on an
   invalid home value, a committed literal appearing in `exc_info`, and
   `config show` accepting invalid home data.
2. Migration snapshot/validation/equality: seven failures showed recursive
   `bool == int == float` no-ops, repeated committed fetches for create/no-op,
   and invalid candidate/simulated effective data reaching publication.
3. Paths/filesystem: dotted `api.v2` resolved to `api.yml`; a symlinked owner
   redirected publication; FIFO/socket entries reached `open`.
4. Test isolation: a child pytest process launched with a conflicting operator
   home changed `ReviewService.prepare` from committed `vendor` to
   `home-vendor`.
5. Exception-chain redaction: a malformed YAML literal appeared in
   `traceback.format_exception(HomeConfigError)`.
6. Final review compatibility: `categories: null` was quarantined even though
   `ReviewPolicy.load_data` treats it as a supported clear/reset value.

## GREEN evidence

- Focused layer/config/policy/prepare/index/MCP suite:
  `207 passed in 2.57s`.
- Additional final sanitization group: `63 passed in 0.50s`.
- Full non-integration suite:
  `2720 passed, 1 skipped, 93 deselected, 1 warning in 59.88s`.
- `.venv/bin/ruff check .`: passed.
- `git diff --check`: passed.

## Result

- Invalid known values quarantine their entire home file before merge,
  including values shadowed by later layers; runtime warnings and strict
  exceptions identify only the source and never the literal. Unknown future
  keys remain mergeable.
- Migration fetches committed YAML exactly once and reuses that text for all
  resolver paths, validates candidate/pre/post effective state before the
  exclusive final publish, and performs recursive type-sensitive comparisons.
- Repository names with dots are collision-free. Existing non-regular
  destinations are rejected before open. Components below the configured root
  are traversed with `O_DIRECTORY`/`O_NOFOLLOW` directory descriptors when
  supported, with a checked cross-platform fallback; ancestors outside the
  configured root remain allowed.
- Unit, integration, and subprocess tests begin with an empty temporary XDG
  home unless a test explicitly supplies a home layer.

## Final review

- Accepted: allow `categories: null` in the standalone known-key validator;
  added runtime/strict and migration coverage.
- Rejected: re-resolving and rolling back after publication. PRI-221
  explicitly requires candidate/pre/post validation before publication and
  publication as the final destination mutation. A post-link read would
  violate that ordering and merely move an external replacement race to a
  later instant; the simulated post-state is validated before the exclusive
  no-clobber link.

## Open concerns

- Infrastructure-backed integration tests were intentionally not run; the
  required command excludes them and reported 93 deselections. The integration
  environment fixture still runs after the new XDG-isolation fixture.
- The directory-descriptor path is exercised on the current POSIX platform;
  the lstat-based fallback is covered by deterministic doubles, not a Windows
  host run.
