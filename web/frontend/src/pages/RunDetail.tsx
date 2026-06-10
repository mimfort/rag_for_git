import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { fetchRun, type RunDetail as RunDetailType, type Finding, type UsageStage } from '../api'
import { fmtCost, fmtDatetime, fmtDuration, shortSha } from '../utils'

function StatusBadge({ status }: { status: RunDetailType['status'] }) {
  if (status === 'ok')         return <span className="badge badge-ok">ok</span>
  if (status === 'error')      return <span className="badge badge-error">error</span>
  if (status === 'draft_skip') return <span className="badge badge-draft">draft_skip</span>
  return <span className="badge">{status}</span>
}

function SeverityBadge({ sev }: { sev: string }) {
  const cls =
    sev === 'critical' ? 'badge-critical' :
    sev === 'high'     ? 'badge-high'     :
    sev === 'medium'   ? 'badge-medium'   : 'badge-low'
  return <span className={`badge ${cls}`}>{sev}</span>
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  const color = pct >= 80 ? 'var(--green)' : pct >= 50 ? 'var(--amber)' : 'var(--gray)'
  return (
    <div className="confidence-bar-wrap">
      <div className="confidence-bar">
        <div className="confidence-bar-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="confidence-val">{pct}%</span>
    </div>
  )
}

const STAGE_LABELS: Record<string, string> = {
  analyze:    'Анализ',
  verify:     'Верификация',
  synthesize: 'Синтез',
}

function UsageTable({ usage, totalCost }: { usage: Record<string, UsageStage>; totalCost: number }) {
  const stages = Object.entries(usage)
  if (stages.length === 0) return <div className="text-muted" style={{ padding: '14px 18px', fontSize: 13 }}>Нет данных</div>

  const totals = stages.reduce(
    (acc, [, s]) => ({
      calls:             acc.calls + s.calls,
      input_tokens:      acc.input_tokens + s.input_tokens,
      output_tokens:     acc.output_tokens + s.output_tokens,
      cache_read_tokens: acc.cache_read_tokens + s.cache_read_tokens,
    }),
    { calls: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0 },
  )

  return (
    <div style={{ overflowX: 'auto' }}>
      <table>
        <thead>
          <tr>
            <th>Этап</th>
            <th>Вызовов</th>
            <th>Вход (токенов)</th>
            <th>Выход (токенов)</th>
            <th>Кэш (токенов)</th>
            <th>Стоимость</th>
          </tr>
        </thead>
        <tbody>
          {stages.map(([stage, s]) => (
            <tr key={stage}>
              <td className="td-mono" style={{ color: 'var(--text-primary)' }}>
                {STAGE_LABELS[stage] ?? stage}
              </td>
              <td className="td-mono" style={{ color: 'var(--text-secondary)' }}>{s.calls}</td>
              <td className="td-mono" style={{ color: 'var(--text-secondary)' }}>
                {s.input_tokens.toLocaleString('ru-RU')}
              </td>
              <td className="td-mono" style={{ color: 'var(--text-secondary)' }}>
                {s.output_tokens.toLocaleString('ru-RU')}
              </td>
              <td className="td-mono" style={{ color: 'var(--blue)' }}>
                {s.cache_read_tokens.toLocaleString('ru-RU')}
              </td>
              <td className="td-mono" style={{ color: 'var(--amber)' }}>
                {fmtCost(s.cost)}
              </td>
            </tr>
          ))}
          <tr className="usage-row-total">
            <td className="td-mono">Итого</td>
            <td className="td-mono">{totals.calls}</td>
            <td className="td-mono">{totals.input_tokens.toLocaleString('ru-RU')}</td>
            <td className="td-mono">{totals.output_tokens.toLocaleString('ru-RU')}</td>
            <td className="td-mono">{totals.cache_read_tokens.toLocaleString('ru-RU')}</td>
            <td className="td-mono">{fmtCost(totalCost)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}

function FindingsTable({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return (
      <div className="state-center" style={{ padding: '32px 24px' }}>
        <div className="state-icon" style={{ fontSize: 24 }}>○</div>
        <div className="state-msg">Нет находок</div>
      </div>
    )
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table>
        <thead>
          <tr>
            <th>Файл : Строка</th>
            <th>Категория</th>
            <th>Серьёзность</th>
            <th>Уверенность</th>
            <th>Вердикт</th>
            <th>Размещение</th>
            <th>Сообщение</th>
          </tr>
        </thead>
        <tbody>
          {findings.map(f => (
            <tr key={f.id}>
              <td className="td-mono" style={{ color: 'var(--blue)', fontSize: 11 }}>
                {f.file}{f.line != null && <span style={{ color: 'var(--text-muted)' }}>:{f.line}</span>}
              </td>
              <td>
                <span className="mono" style={{ fontSize: 12 }}>{f.category}</span>
              </td>
              <td><SeverityBadge sev={f.severity} /></td>
              <td><ConfidenceBar value={f.confidence} /></td>
              <td>
                {f.is_real
                  ? <span style={{ color: 'var(--green)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>реальная</span>
                  : <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>отклонена</span>
                }
              </td>
              <td>
                {f.inline
                  ? <span style={{ color: 'var(--amber)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>inline</span>
                  : <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>сводка</span>
                }
              </td>
              <td style={{ maxWidth: 320, fontSize: 12, color: 'var(--text-secondary)' }}>
                <span className="truncate" style={{ display: 'block' }}>{f.message}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function RunDetail() {
  const { id } = useParams<{ id: string }>()
  const [run, setRun] = useState<RunDetailType | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    setLoading(true)
    setError(null)
    fetchRun(Number(id))
      .then(setRun)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [id])

  if (loading) {
    return (
      <div className="state-center">
        <div className="spinner" />
        <div className="state-msg">Загрузка прогона…</div>
      </div>
    )
  }

  if (error) {
    return (
      <div>
        <Link to="/runs" className="back-link">← Прогоны</Link>
        <div className="state-center">
          <div className="state-icon">!</div>
          <div className="state-msg">Ошибка</div>
          <div className="state-detail">{error}</div>
        </div>
      </div>
    )
  }

  if (!run) return null

  const ghUrl = `https://github.com/${run.repo}/pull/${run.pr_number}`

  return (
    <div>
      <Link to="/runs" className="back-link">← Прогоны</Link>

      {/* Шапка прогона */}
      <div className="detail-header">
        <div className="detail-title-row">
          <a href={ghUrl} target="_blank" rel="noopener noreferrer" className="detail-pr-link">
            {run.repo}#{run.pr_number}
          </a>
          <StatusBadge status={run.status} />
          {run.dry_run && <span className="badge badge-dry">dry-run</span>}
        </div>

        <div className="detail-meta-grid">
          <div className="detail-meta-item">
            <span className="meta-label">Создан</span>
            <span className="meta-value">{fmtDatetime(run.created_at)}</span>
          </div>
          <div className="detail-meta-item">
            <span className="meta-label">Base SHA</span>
            <span className="meta-value">{shortSha(run.base_sha)}</span>
          </div>
          <div className="detail-meta-item">
            <span className="meta-label">Head SHA</span>
            <span className="meta-value">{shortSha(run.head_sha)}</span>
          </div>
          <div className="detail-meta-item">
            <span className="meta-label">Модель (анализ)</span>
            <span className="meta-value">{run.model}</span>
          </div>
          {run.model_verify && (
            <div className="detail-meta-item">
              <span className="meta-label">Модель (verify)</span>
              <span className="meta-value">{run.model_verify}</span>
            </div>
          )}
          <div className="detail-meta-item">
            <span className="meta-label">Длительность</span>
            <span className="meta-value">{fmtDuration(run.duration_ms)}</span>
          </div>
          <div className="detail-meta-item">
            <span className="meta-label">Файлы</span>
            <span className="meta-value">
              {run.files_reviewed} обработано
              {run.files_skipped > 0 && `, ${run.files_skipped} пропущено`}
              {run.files_failed > 0 && `, ${run.files_failed} с ошибкой`}
            </span>
          </div>
          <div className="detail-meta-item">
            <span className="meta-label">Находки</span>
            <span className="meta-value">
              {run.findings_kept} из {run.findings_analyzed} (отсев: {run.verify_rejected})
            </span>
          </div>
          <div className="detail-meta-item">
            <span className="meta-label">Комментарии</span>
            <span className="meta-value">
              {run.comments_inline} inline, {run.comments_summary} в сводке
            </span>
          </div>
          <div className="detail-meta-item">
            <span className="meta-label">Итого $</span>
            <span className="meta-value" style={{ color: 'var(--amber)' }}>
              {fmtCost(run.total_cost)}
            </span>
          </div>
        </div>
      </div>

      {/* Ошибка */}
      {run.error_text && (
        <div className="error-block">
          <div className="error-block-title">Текст ошибки</div>
          {run.error_text}
        </div>
      )}

      {/* Использование токенов */}
      <div className="section-card">
        <div className="section-card-header">
          <div className="section-title" style={{ margin: 0, border: 0, padding: 0 }}>
            Использование токенов по этапам
          </div>
        </div>
        <UsageTable usage={run.usage} totalCost={run.total_cost} />
      </div>

      {/* Находки */}
      <div className="section-card">
        <div className="section-card-header">
          <div className="section-title" style={{ margin: 0, border: 0, padding: 0 }}>
            Находки ({run.findings.length})
          </div>
        </div>
        <FindingsTable findings={run.findings} />
      </div>
    </div>
  )
}
