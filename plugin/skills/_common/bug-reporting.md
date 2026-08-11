**Noticed a defect of reviewer itself?** If a reviewer MCP tool broke its own documented
contract, a skill step was impossible with the available tools, a stated invariant failed
(idempotency, dedup, overlay cleanup, outcome counts), or a reviewer frame appeared in a
traceback — do not fix it silently and move on. Tell the user in one line and offer the
`report-bug` skill, which assembles an anonymized issue for `mimfort/rag_for_git`.

Stay silent about everything that is not the tool's defect: unavailable Postgres/Neo4j, missing
keys, a stale index, board or Voyage rate limits, GitHub/GitLab errors, the user's own failing
tests or git conflicts, permission denials. The channel is only worth having while it is quiet
on other people's problems.

Never publish anything yourself: publication happens only through `report_bug` with an explicit
human approval, and never at all in headless, cron or background runs.
