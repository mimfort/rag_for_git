# Client compatibility

Reviewer skills use the same MCP tools in Codex, Claude Code, and other MCP clients. Tool availability depends on the connected reviewer server version. Claude Code's native hooks additionally handle automatic transcript capture and diagnostic reminders.

## Verify solve-task citations

On the same MCP connection, call `prepare_task_context` with `include_brief_evidence=true`. Use the returned `brief_evidence_id` once you have the brief's citations:

```json
{
  "evidence_id": "<brief_evidence_id>",
  "citations": [
    {"section": "code", "path": "reviewer/tasks/sync.py", "line": 67}
  ]
}
```

Send that object to `verify_task_brief`. Use `test_exemplars` for citations from the test section. Pass paths and line numbers from the visible excerpts. The tool returns `verified`, `path_unseen`, `outside_excerpt`, or `section_unavailable` per citation. The token can expire or become unavailable after a server restart. Continue inspecting the captured tool output in that case. The server retains an index of numbered rows and never needs a second retrieval to check them.

## Measure token usage

```bash
reviewer measure-client-usage --source codex-exec --model gpt-6-luna events.jsonl
reviewer measure-client-usage --source claude-code transcript.jsonl
reviewer measure-client-usage --source normalized < usage.jsonl
```

For Codex, record standard output from `codex exec --json` and use completed turns. The adapter separates cached input from the total and counts output once, including reasoning. Claude Code already reports the four components. For repeated Claude snapshots with the same message ID, the last usage record wins. Other clients and SDKs can export one completed usage record per line in this format:

```json
{"type":"llm_usage","model":"example","usage":{"fresh_in":100,"output":20,"cache_write":0,"cache_read":50}}
```

The command reports totals, model breakdown, and the number of records. It reads only the supplied input. Scope the input to the task or stage being measured. Capturing completed usage and collecting ongoing turn usage depend on what the client exposes.

## Diagnose reviewer results

When a reviewer result looks inconsistent, call `diagnose_tool_result` with its logical tool name and the error/status envelope or traceback as a JSON string. If the result matches a reviewer-specific traceback or a known status contract violation, the tool returns `suspected_defect`, a kind, and a signature. Examples of ordinary infrastructure failures return `clear`. This operation is read-only. A distinct, explicitly approved report-bug action can publish a report.
