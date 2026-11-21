# Client compatibility

Reviewer skills use the same MCP tools in Codex, Claude Code, and other MCP clients. Tool availability depends on the connected reviewer server version. Native hooks can emit diagnostic reminders. Claude Code captures usage from its transcript, and Codex can capture usage from model request events.

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

## Inspect code budget

Set `include_code_budget=true` on `prepare_task_context` for counts from the existing retrieval. The response's `code_budget` reports distinct candidate paths after graph expansion and test filtering, paths omitted by the file limit before augmentation, and paths displaced by augmentation. It compares the final selected files with the files that have a complete numbered row in the rendered code section. These counts cover all candidate paths. The response omits the field if every search failed.

## Measure token usage

```bash
reviewer measure-client-usage --source codex-exec --model gpt-6-luna events.jsonl
reviewer measure-client-usage --source codex-app-server --model gpt-6-luna notifications.jsonl
reviewer measure-client-usage --source codex-hook requests.jsonl
reviewer measure-client-usage --source claude-code transcript.jsonl
reviewer measure-client-usage --source normalized < usage.jsonl
```

For batch Codex runs, capture standard output from `codex exec --json`. App Server clients can write JSON-RPC notifications in arrival order. The adapter reads `thread/tokenUsage/updated`, takes the first visible request for each thread and then each increase in cumulative usage. Repeated snapshots count once. Capture a contiguous interval per thread. An increase between snapshots includes any requests between them. Use `--model` for a single-model capture because App Server usage notifications omit the model name. Both Codex adapters separate cached input from total input and count reasoning within output.

Claude Code already reports the four components. For repeated Claude snapshots with the same message ID, the last usage record wins. Other clients and SDKs can export one completed usage record per line in this format.

```json
{"type":"llm_usage","model":"example","usage":{"fresh_in":100,"output":20,"cache_write":0,"cache_read":50}}
```

The command reports totals, model breakdown, and the number of records. It reads only the supplied input. Scope the input to the task or stage being measured. Capturing completed usage and collecting ongoing turn usage depend on what the client exposes.

### Automatic Codex capture

With the Codex plugin enabled and its `PostModelRequest` hook trusted, select a local output file for one solve-task run.

```bash
reviewer_usage_file=$(mktemp)
REVIEWER_CODEX_USAGE_FILE="$reviewer_usage_file" codex exec -m gpt-6-luna 'Use rag-reviewer:solve-task for PRI-278. Produce the brief.'
reviewer measure-client-usage --source codex-hook "$reviewer_usage_file"
```

The hook appends reported per-request counters and their model, session, turn, and status. It omits prompts, tool inputs, and error details. A separate file per invocation keeps review and solve-task runs separate. The hook is idle when `REVIEWER_CODEX_USAGE_FILE` is unset. Native capture was exercised with Codex CLI 0.170.0 on macOS.

## Diagnose reviewer results

When a reviewer result looks inconsistent, call `diagnose_tool_result` with its logical tool name and the result or traceback as a JSON string. Complete reviewer MCP tool events from `codex exec --json` and App Server are accepted directly. If the result matches a reviewer-specific traceback or a known status contract violation, the tool returns `suspected_defect`, a kind, and a signature. Ordinary external failures such as authentication errors return `clear`. This operation is read-only. A distinct, explicitly approved report-bug action can publish a report.
