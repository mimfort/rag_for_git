import { useState, useEffect } from 'react'
import { getTrace, type TraceStep, type ToolCallEntry, type StageBreakdown } from '../api'
import { fmtCost } from '../utils'

// ─── Helpers ─────────────────────────────────────────────────────────────────

// Стадии двух каналов пересекаются, но не совпадают: серверный трейс знает
// analyze/verify/synthesize/client, клиентский хук (usage.by_stage) — более
// мелкие стадии окна ревью. Один словарь на оба источника.
const STAGE_LABELS: Record<string, string> = {
  analyze:        'Анализ',
  verify:         'Верификация',
  synthesize:     'Синтез',
  client:         'Клиент',
  orchestrator:   'Оркестратор',
  risk:           'Риски',
  blast_radius:   'Радиус поражения',
  requirements:   'Требования',
  performance:    'Производительность',
  maintainability:'Поддерживаемость',
  subagent:       'Субагент',
}

const STAGE_ORDER = [
  'analyze', 'verify', 'synthesize', 'client', 'orchestrator',
  'risk', 'blast_radius', 'requirements', 'performance', 'maintainability', 'subagent',
]

function sortStages<T>(map: Map<string, T>): string[] {
  return [
    ...STAGE_ORDER.filter(s => map.has(s)),
    ...[...map.keys()].filter(s => !STAGE_ORDER.includes(s)),
  ]
}

function stageBarClass(stage: string) {
  if (stage === 'analyze')    return 'trace-stage-bar trace-stage-bar-analyze'
  if (stage === 'verify')     return 'trace-stage-bar trace-stage-bar-verify'
  if (stage === 'synthesize') return 'trace-stage-bar trace-stage-bar-synthesize'
  return 'trace-stage-bar trace-stage-bar-other'
}

function dotClass(kind: TraceStep['kind']) {
  if (kind === 'llm_call')  return 'trace-step-dot trace-step-dot-llm'
  if (kind === 'tool_call') return 'trace-step-dot trace-step-dot-tool'
  return 'trace-step-dot trace-step-dot-prompt'
}

/** Parse search_code result lines into RAG chunks:
 *  `// path#fqn (file.py:10-20)` */
function parseRagChunks(text: string): { nodeId: string; path: string }[] {
  const lines = text.split('\n')
  const re = /^\/\/\s+([^\s(]+)\s+\(([^)]+)\)/
  return lines
    .map(l => { const m = l.match(re); return m ? { nodeId: m[1], path: m[2] } : null })
    .filter((x): x is { nodeId: string; path: string } => x !== null)
}

// ─── Sub-components ──────────────────────────────────────────────────────────

function ToolChip({ tc }: { tc: ToolCallEntry }) {
  const argsStr = Object.entries(tc.args)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(', ')
  return (
    <span className="tool-chip">
      <span className="tool-chip-arrow">→</span>
      {tc.name}
      {argsStr && <span style={{ color: 'rgba(169,139,255,0.6)', marginLeft: 3 }}>({argsStr})</span>}
    </span>
  )
}

function PromptStep({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="step-card">
      <div className="step-card-header" onClick={() => setOpen(o => !o)}>
        <span className="step-kind-label step-kind-label-prompt">Промпт</span>
        {step.name && (
          <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: 'var(--text-muted)', marginLeft: 6 }}>
            {step.name}
          </span>
        )}
        <div className="step-card-meta">
          <span className="step-expand-icon" style={open ? { transform: 'rotate(180deg)' } : {}}>▾</span>
        </div>
      </div>
      {open && (
        <div className="step-card-body">
          <div className="prompt-text">{step.text ?? '(пусто)'}</div>
        </div>
      )}
    </div>
  )
}

function LlmCallStep({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const text = step.text ?? ''
  const long = text.length > 280

  return (
    <div className="step-card">
      <div className="step-card-header" onClick={() => setOpen(o => !o)}>
        <span className="step-kind-label step-kind-label-llm">LLM</span>
        {step.name && (
          <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: 'var(--text-muted)', marginLeft: 6 }}>
            {step.name}
          </span>
        )}
        <div className="step-card-meta">
          {step.tokens != null && (
            <span className="step-tokens">{step.tokens.toLocaleString('ru-RU')} tok</span>
          )}
          {step.cost != null && step.cost > 0 && (
            <span className="step-cost">{fmtCost(step.cost)}</span>
          )}
          <span className="step-expand-icon" style={open ? { transform: 'rotate(180deg)' } : {}}>▾</span>
        </div>
      </div>
      {open && (
        <div className="step-card-body">
          {text && (
            <>
              <div className={`llm-reasoning${!expanded && long ? ' collapsed' : ''}`}>
                {text}
              </div>
              {long && (
                <button className="expand-btn" onClick={e => { e.stopPropagation(); setExpanded(x => !x) }}>
                  {expanded ? 'Свернуть' : 'Показать полностью'}
                </button>
              )}
            </>
          )}
          {step.tool_calls && step.tool_calls.length > 0 && (
            <div className="tool-calls-row">
              {step.tool_calls.map((tc, i) => <ToolChip key={i} tc={tc} />)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ToolCallStep({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(false)
  const [showAll, setShowAll] = useState(false)

  const tc = step.tool_calls?.[0]
  const toolName = tc?.name ?? step.name ?? 'tool'
  const argsText = tc?.args ? JSON.stringify(tc.args, null, 2) : null
  const rawResult = step.text ?? ''

  // If it's search_code — try to parse RAG chunks
  const isSearchCode = toolName === 'search_code'
  const chunks = isSearchCode ? parseRagChunks(rawResult) : []
  const hasExtra = chunks.length > 5
  const visibleChunks = showAll ? chunks : chunks.slice(0, 5)

  return (
    <div className="step-card">
      <div className="step-card-header" onClick={() => setOpen(o => !o)}>
        <span className="step-kind-label step-kind-label-tool">Инструмент</span>
        <span className="badge badge-teal" style={{ marginLeft: 8, fontSize: 10 }}>{toolName}</span>
        <div className="step-card-meta">
          <span className="step-expand-icon" style={open ? { transform: 'rotate(180deg)' } : {}}>▾</span>
        </div>
      </div>
      {open && (
        <div className="step-card-body">
          {argsText && (
            <div className="tool-args-block">{argsText}</div>
          )}
          {rawResult && (
            <>
              <div className="tool-result-header">
                <span className="tool-result-label">Результат</span>
              </div>
              {isSearchCode && chunks.length > 0 ? (
                <>
                  <div className="rag-chunks-list">
                    {visibleChunks.map((c, i) => (
                      <div key={i} className="rag-chunk-item">
                        <span className="rag-chunk-node">{c.nodeId}</span>
                        <span className="rag-chunk-path">{c.path}</span>
                      </div>
                    ))}
                  </div>
                  {hasExtra && (
                    <button className="expand-btn" onClick={e => { e.stopPropagation(); setShowAll(x => !x) }}>
                      {showAll ? 'Свернуть' : `Показать ещё ${chunks.length - 5}`}
                    </button>
                  )}
                </>
              ) : (
                <div className="tool-result-raw">{rawResult}</div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function TraceStepItem({ step, isLast }: { step: TraceStep; isLast: boolean }) {
  return (
    <div className="trace-step" style={{ animationDelay: `${step.seq * 0.03}s` }}>
      <div className="trace-step-connector">
        <div className={dotClass(step.kind)} />
        {!isLast && <div className="trace-step-line" />}
      </div>
      <div className="trace-step-content">
        {step.kind === 'prompt'   && <PromptStep  step={step} />}
        {step.kind === 'llm_call' && <LlmCallStep step={step} />}
        {step.kind === 'tool_call'&& <ToolCallStep step={step} />}
      </div>
    </div>
  )
}

function UnitAccordion({ unit, steps }: { unit: string; steps: TraceStep[] }) {
  const [open, setOpen] = useState(true)
  return (
    <div className="trace-unit-accordion">
      <div className="trace-unit-header" onClick={() => setOpen(o => !o)}>
        <span className="trace-unit-icon">{open ? '▾' : '▸'}</span>
        <span className="trace-unit-name">{unit}</span>
        <span className="trace-unit-count">{steps.length} шагов</span>
      </div>
      {open && (
        <div className="trace-timeline">
          {steps.map((step, i) => (
            <TraceStepItem key={step.seq} step={step} isLast={i === steps.length - 1} />
          ))}
        </div>
      )}
    </div>
  )
}

function StageSection({ stage, steps }: { stage: string; steps: TraceStep[] }) {
  const [open, setOpen] = useState(true)

  // Group by unit
  const unitMap = new Map<string, TraceStep[]>()
  for (const s of steps) {
    const u = s.unit || '(синтез)'
    if (!unitMap.has(u)) unitMap.set(u, [])
    unitMap.get(u)!.push(s)
  }

  const totalTokens = steps.reduce((a, s) => a + (s.tokens ?? 0), 0)
  const totalCost   = steps.reduce((a, s) => a + (s.cost ?? 0), 0)

  return (
    <div className="trace-stage-section">
      <div className="trace-stage-header" onClick={() => setOpen(o => !o)}>
        <div className={stageBarClass(stage)} />
        <div className="trace-stage-name">{STAGE_LABELS[stage] ?? stage}</div>
        <div className="trace-stage-meta">
          {totalTokens > 0 && (
            <span style={{ marginRight: 12 }}>{totalTokens.toLocaleString('ru-RU')} tok</span>
          )}
          {totalCost > 0 && (
            <span style={{ color: 'var(--amber)' }}>{fmtCost(totalCost)}</span>
          )}
        </div>
        <span className={`trace-chevron${open ? ' open' : ''}`}>▶</span>
      </div>
      {open && (
        <div className="trace-stage-body">
          {[...unitMap.entries()].map(([unit, usteps]) => (
            <UnitAccordion key={unit} unit={unit} steps={usteps} />
          ))}
        </div>
      )}
    </div>
  )
}

function fmtKb(bytes: number): string {
  return (bytes / 1024).toLocaleString('ru-RU', { maximumFractionDigits: 1 })
}

/** Разрез трейса по стадиям: шаги/payload — из серверного трейса, расход —
 *  из usage.by_stage (клиентский хук). Стадия без данных о расходе — прочерк,
 *  а не 0: пустая ячейка честнее нуля. */
function StageBreakdownTable({ rows }: { rows: StageBreakdown[] }) {
  if (rows.length === 0) return null
  const order = sortStages(new Map(rows.map(r => [r.stage, r])))
  return (
    <table className="stage-breakdown-table">
      <thead>
        <tr>
          <th>Стадия</th>
          <th>Шагов</th>
          <th>Отдано, КБ</th>
          <th>Получено, КБ</th>
          <th>Расход, усл. ед.</th>
        </tr>
      </thead>
      <tbody>
        {order.map(stage => {
          const r = rows.find(row => row.stage === stage)!
          return (
            <tr key={stage}>
              <td>{STAGE_LABELS[stage] ?? stage}</td>
              <td>{r.steps}</td>
              <td>{fmtKb(r.args_bytes)}</td>
              <td>{fmtKb(r.result_bytes)}</td>
              <td>{r.cost != null ? r.cost.toLocaleString('ru-RU', { maximumFractionDigits: 2 }) : '—'}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

// ─── Main TraceView ──────────────────────────────────────────────────────────

export default function TraceView({ runId }: { runId: number }) {
  const [steps, setSteps]   = useState<TraceStep[] | null>(null)
  const [byStage, setByStage] = useState<StageBreakdown[]>([])
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    getTrace(runId)
      .then(r => { setSteps(r.steps); setByStage(r.by_stage ?? []) })
      .catch((e: Error) => {
        // 404 → trace simply not available (no REVIEW_TRACE or old run)
        if (e.message.includes('404') || e.message.toLowerCase().includes('not found')) {
          setSteps([])
        } else {
          setError(e.message)
        }
      })
      .finally(() => setLoading(false))
  }, [runId])

  if (loading) {
    return (
      <div className="state-center">
        <div className="spinner" />
        <div className="state-msg">Загрузка трейса…</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="state-center">
        <div className="state-icon">⚠</div>
        <div className="state-msg">Ошибка загрузки трейса</div>
        <div className="state-detail">{error}</div>
      </div>
    )
  }

  if (!steps || steps.length === 0) {
    return (
      <div className="trace-empty">
        <div className="trace-empty-icon">⋯</div>
        <div className="trace-empty-title">Трейс недоступен</div>
        <div className="trace-empty-desc">
          Прогон выполнен без трассировки (старый прогон или{' '}
          <span style={{ fontFamily: 'JetBrains Mono, monospace', color: 'var(--indigo)' }}>REVIEW_TRACE=false</span>).
        </div>
      </div>
    )
  }

  // Group by stage, preserve order
  const stageMap = new Map<string, TraceStep[]>()
  for (const s of steps) {
    if (!stageMap.has(s.stage)) stageMap.set(s.stage, [])
    stageMap.get(s.stage)!.push(s)
  }

  // Sort stages: known order first, then rest
  const sortedStages = sortStages(stageMap)

  return (
    <div className="trace-root">
      <StageBreakdownTable rows={byStage} />
      {sortedStages.map(stage => (
        <StageSection key={stage} stage={stage} steps={stageMap.get(stage)!} />
      ))}
    </div>
  )
}
