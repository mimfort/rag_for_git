// ─── Типы ─────────────────────────────────────────────────────────────────────

export interface UsageStage {
  calls: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cost: number
}

export interface Run {
  id: number
  created_at: string
  repo: string
  pr_number: number
  base_sha: string
  head_sha: string
  model: string
  model_verify: string | null
  dry_run: boolean
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  status: 'ok' | 'error' | 'draft_skip'
  files_reviewed: number
  files_skipped: number
  files_failed: number
  findings_analyzed: number
  findings_kept: number
  verify_rejected: number
  comments_inline: number
  comments_summary: number
  usage: Record<string, UsageStage>
  total_cost: number
  error_text: string | null
}

export interface Finding {
  id: number
  file: string
  line: number | null
  category: string
  severity: 'critical' | 'high' | 'medium' | 'low'
  confidence: number
  is_real: boolean
  published: boolean
  inline: boolean
  fingerprint: string
  message: string
}

export interface RunDetail extends Run {
  findings: Finding[]
}

export interface RunsResponse {
  runs: Run[]
  limit: number
  offset: number
}

export interface CostPoint {
  date: string
  cost: number
}

export interface RunsPoint {
  date: string
  count: number
}

export interface CategoryPoint {
  category: string
  count: number
}

export interface SeverityPoint {
  severity: string
  count: number
}

export interface Stats {
  total_runs: number
  total_cost: number
  avg_cost_per_run: number
  total_findings: number
  verify_reject_rate: number
  cost_over_time: CostPoint[]
  runs_over_time: RunsPoint[]
  findings_by_category: CategoryPoint[]
  findings_by_severity: SeverityPoint[]
}

// ─── API-клиент ───────────────────────────────────────────────────────────────

async function apiFetch<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error((body as { detail?: string }).detail ?? res.statusText)
  }
  return res.json() as Promise<T>
}

export async function fetchRuns(params: {
  repo?: string
  status?: string
  limit?: number
  offset?: number
}): Promise<RunsResponse> {
  const q = new URLSearchParams()
  if (params.repo) q.set('repo', params.repo)
  if (params.status) q.set('status', params.status)
  if (params.limit != null) q.set('limit', String(params.limit))
  if (params.offset != null) q.set('offset', String(params.offset))
  return apiFetch<RunsResponse>(`/api/runs?${q.toString()}`)
}

export async function fetchRun(id: number): Promise<RunDetail> {
  return apiFetch<RunDetail>(`/api/runs/${id}`)
}

export async function fetchStats(days: number): Promise<Stats> {
  return apiFetch<Stats>(`/api/stats?days=${days}`)
}

// ─── Trace API ────────────────────────────────────────────────────────────────

export interface ToolCallEntry {
  name: string
  args: Record<string, unknown>
}

export interface TraceStep {
  seq: number
  stage: string
  unit: string
  kind: 'prompt' | 'llm_call' | 'tool_call'
  name: string | null
  text: string | null
  tool_calls: ToolCallEntry[] | null
  tokens: number | null
  cost: number | null
  created_at: string
}

export interface TraceResponse {
  steps: TraceStep[]
}

export async function getTrace(id: number): Promise<TraceResponse> {
  return apiFetch<TraceResponse>(`/api/runs/${id}/trace`)
}
