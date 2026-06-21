Findings output schema (shared). The calling skill sets `category`.

Return ONLY a JSON object (no prose around it):

```json
{"findings": [{
  "category": "<set by the calling skill>",
  "severity": "low|medium|high|critical",
  "file": "<path of the reviewed file>",
  "line": <line number in the NEW file, or null>,
  "side": "RIGHT|LEFT",
  "code_quote": "<exact line from the new file, or null when line is null>",
  "message": "<what is wrong and why it matters>",
  "suggestion": "<short advice or null>",
  "fix": {"start_line": N, "end_line": M, "replacement": "<new code>"} | null,
  "confidence": 0.0
}]}
```

Field semantics:
- `category` — set by the calling skill (see its own instructions).
- `severity` — `low|medium|high|critical`.
- `file` — path of the reviewed file.
- `line` — line number in the NEW file, or `null` (a null line lands in the summary, not inline).
- `side` — `RIGHT` (new version) or `LEFT` (old version).
- `code_quote` — exact line copied verbatim from the NEW file; it grounds the line number. `null` only when `line` is null. An inaccurate quote is worse than no quote.
- `message` / `suggestion` — written in the orchestrator's output language.
- `fix` — exact replacement for a contiguous line range in the new file (`start_line`/`end_line`/`replacement`), or `null` when unsure.
- `confidence` — float `0.0..1.0`; it feeds the publish gate, so be honest.

Write `message` and `suggestion` in the output language given by the orchestrator.
