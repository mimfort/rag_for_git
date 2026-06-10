import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchRuns, type Run } from '../api'
import { fmtCost, fmtDatetime, fmtDuration } from '../utils'

const LIMIT = 50

function StatusBadge({ status }: { status: Run['status'] }) {
  if (status === 'ok')         return <span className="badge badge-ok">ok</span>
  if (status === 'error')      return <span className="badge badge-error">error</span>
  if (status === 'draft_skip') return <span className="badge badge-draft">draft</span>
  return <span className="badge">{status}</span>
}

export default function Runs() {
  const navigate = useNavigate()
  const [runs, setRuns] = useState<Run[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)

  const [filterRepo,   setFilterRepo]   = useState('')
  const [filterStatus, setFilterStatus] = useState('')

  const load = useCallback((off: number) => {
    setLoading(true)
    setError(null)
    fetchRuns({
      repo:   filterRepo   || undefined,
      status: filterStatus || undefined,
      limit:  LIMIT,
      offset: off,
    })
      .then(r => {
        setRuns(r.runs)
        setHasMore(r.runs.length === LIMIT)
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [filterRepo, filterStatus])

  useEffect(() => {
    setOffset(0)
    load(0)
  }, [load])

  const goNext = () => {
    const next = offset + LIMIT
    setOffset(next)
    load(next)
  }

  const goPrev = () => {
    const prev = Math.max(0, offset - LIMIT)
    setOffset(prev)
    load(prev)
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Прогоны</div>
        <div className="page-subtitle">История запусков агента ревью</div>
      </div>

      <div className="table-card">
        <div className="table-toolbar">
          <span className="toolbar-title">Фильтры</span>
          <input
            className="input-field"
            placeholder="owner/repo"
            value={filterRepo}
            onChange={e => setFilterRepo(e.target.value)}
            style={{ width: 180 }}
          />
          <select
            className="select-field"
            value={filterStatus}
            onChange={e => setFilterStatus(e.target.value)}
          >
            <option value="">Все статусы</option>
            <option value="ok">ok</option>
            <option value="error">error</option>
            <option value="draft_skip">draft_skip</option>
          </select>
        </div>

        {loading ? (
          <div className="state-center">
            <div className="spinner" />
            <div className="state-msg">Загрузка прогонов…</div>
          </div>
        ) : error ? (
          <div className="state-center">
            <div className="state-icon">!</div>
            <div className="state-msg">Ошибка загрузки</div>
            <div className="state-detail">{error}</div>
          </div>
        ) : runs.length === 0 ? (
          <div className="state-center">
            <div className="state-icon">○</div>
            <div className="state-msg">Прогонов не найдено</div>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>Время</th>
                  <th>Репо / PR</th>
                  <th>Модель</th>
                  <th>Статус</th>
                  <th title="reviewed / skipped / failed">Файлы</th>
                  <th title="kept / analyzed">Находки</th>
                  <th title="inline / summary">Коммент.</th>
                  <th>Длит.</th>
                  <th>Стоимость</th>
                </tr>
              </thead>
              <tbody>
                {runs.map(run => (
                  <tr
                    key={run.id}
                    className="clickable"
                    onClick={() => navigate(`/runs/${run.id}`)}
                  >
                    <td className="td-mono" style={{ color: 'var(--text-secondary)', fontSize: 11 }}>
                      {fmtDatetime(run.created_at)}
                    </td>
                    <td>
                      <span className="mono" style={{ color: 'var(--blue)', fontSize: 12 }}>
                        {run.repo}<span style={{ color: 'var(--text-muted)' }}>#{run.pr_number}</span>
                      </span>
                      {run.dry_run && (
                        <span className="badge badge-dry" style={{ marginLeft: 6 }}>dry-run</span>
                      )}
                    </td>
                    <td>
                      <span className="mono" style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                        {run.model.length > 30 ? run.model.slice(0, 28) + '…' : run.model}
                      </span>
                    </td>
                    <td><StatusBadge status={run.status} /></td>
                    <td className="td-mono">
                      <span style={{ color: 'var(--green)' }}>{run.files_reviewed}</span>
                      <span style={{ color: 'var(--text-muted)' }}>
                        {run.files_skipped > 0 && ` +${run.files_skipped}ск`}
                        {run.files_failed > 0 && ` ${run.files_failed}ош`}
                      </span>
                    </td>
                    <td className="td-mono">
                      <span style={{ color: 'var(--amber)' }}>{run.findings_kept}</span>
                      <span style={{ color: 'var(--text-muted)' }}>/{run.findings_analyzed}</span>
                    </td>
                    <td className="td-mono" style={{ color: 'var(--text-secondary)' }}>
                      {run.comments_inline}i / {run.comments_summary}с
                    </td>
                    <td className="td-mono" style={{ color: 'var(--text-secondary)' }}>
                      {fmtDuration(run.duration_ms)}
                    </td>
                    <td className="td-mono" style={{ color: 'var(--amber)' }}>
                      {fmtCost(run.total_cost)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="pagination">
          <span className="pagination-info">
            {runs.length > 0 ? `${offset + 1}–${offset + runs.length}` : '0'} записей
          </span>
          <button className="btn" onClick={goPrev} disabled={offset === 0}>
            ← Назад
          </button>
          <button className="btn" onClick={goNext} disabled={!hasMore}>
            Вперёд →
          </button>
        </div>
      </div>
    </div>
  )
}
