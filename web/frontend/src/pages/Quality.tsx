import { useState, useEffect } from 'react'
import {
  LineChart, Line, BarChart, Bar, ReferenceLine,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts'
import { fetchQuality, type Quality } from '../api'
import { fmtPct } from '../utils'

const PERIODS = [
  { label: '30 дн', value: 30 },
  { label: '90 дн', value: 90 },
  { label: '365 дн', value: 365 },
]

// База «до» из офлайн-харнесса PRI-250 на bulk-подвыборке (коммит d474e02):
// bulk_core_recall_median ≈ 0.373 при 4 измеренных задачах. Горизонталь на
// графике — точка отсчёта для отложенного критерия 4 PRI-251.
const BULK_BASELINE = 0.373

const tooltipStyle = {
  background: 'rgba(14,16,24,0.95)',
  border: '1px solid rgba(255,255,255,0.07)',
  borderRadius: 8,
  fontFamily: 'JetBrains Mono, monospace',
  fontSize: 12,
}

export default function Quality() {
  const [days, setDays] = useState(90)
  const [data, setData] = useState<Quality | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetchQuality(days)
      .then(setData)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [days])

  if (loading) {
    return (
      <div className="state-center">
        <div className="spinner" />
        <div className="state-msg">Загрузка…</div>
      </div>
    )
  }
  if (error) {
    return (
      <div className="state-center">
        <div className="state-icon">⚠</div>
        <div className="state-msg">Ошибка загрузки</div>
        <div className="state-detail">{error}</div>
      </div>
    )
  }
  if (!data) return null

  const bulk = data.trend.filter((p) => p.expected_core >= data.bulk_threshold)
  const points = data.trend.map((p) => ({
    date: p.date.slice(0, 10),
    task_key: p.task_key,
    core_recall: p.core_recall,
    precision: p.precision,
    bulk_recall: p.expected_core >= data.bulk_threshold ? p.core_recall : null,
  }))

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Качество брифа solve-task</div>
        <div className="period-selector">
          {PERIODS.map((period) => (
            <button
              key={period.value}
              className={`period-btn${period.value === days ? ' active' : ''}`}
              onClick={() => setDays(period.value)}
            >
              {period.label}
            </button>
          ))}
        </div>
      </div>

      <div className="kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label">core-recall, медиана</div>
          <div className="kpi-value">
            {data.aggregate.core_recall_median == null ? '—' : fmtPct(data.aggregate.core_recall_median)}
          </div>
          <div className="kpi-sub">измерено задач: {data.aggregate.n_measured}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">bulk-подвыборка (ядро ≥ {data.bulk_threshold})</div>
          <div className="kpi-value">
            {data.bulk.core_recall_median == null ? '—' : fmtPct(data.bulk.core_recall_median)}
          </div>
          <div className="kpi-sub">
            задач: {data.bulk.n_measured} · база до family: {fmtPct(BULK_BASELINE)}
          </div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">без точки измерения</div>
          <div className="kpi-value">{data.aggregate.no_measurement}</div>
          <div className="kpi-sub">
            {Object.entries(data.no_measurement_by_status)
              .map(([status, count]) => `${status}: ${count}`)
              .join(' · ') || '—'}
          </div>
        </div>
      </div>

      <section className="chart-card">
        <h2>Динамика</h2>
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={points}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
            <XAxis dataKey="date" stroke="#8b90a5" fontSize={12} />
            <YAxis domain={[0, 1]} stroke="#8b90a5" fontSize={12} />
            <Tooltip contentStyle={tooltipStyle} />
            <Legend />
            <ReferenceLine
              y={BULK_BASELINE}
              stroke="#ff8c42"
              strokeDasharray="4 4"
              label={{ value: 'база bulk до family', fill: '#ff8c42', fontSize: 11 }}
            />
            <Line name="core-recall" dataKey="core_recall" stroke="#2dd4bf" dot />
            <Line name="precision" dataKey="precision" stroke="#7c5cff" dot />
            <Line name="bulk core-recall" dataKey="bulk_recall" stroke="#ff4d6d" dot connectNulls />
          </LineChart>
        </ResponsiveContainer>
        {bulk.length === 0 && (
          <p style={{ fontSize: 12, color: 'var(--text-muted)', fontFamily: 'JetBrains Mono, monospace', marginTop: 8 }}>
            В окне нет задач с ядром ≥ {data.bulk_threshold}: bulk-линия пуста.
          </p>
        )}
      </section>

      <section className="chart-card">
        <h2>Промахи по категориям</h2>
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={data.misses}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
            <XAxis dataKey="category" stroke="#8b90a5" fontSize={11} interval={0} angle={-20} height={70} textAnchor="end" />
            <YAxis stroke="#8b90a5" fontSize={12} />
            <Tooltip contentStyle={tooltipStyle} />
            <Bar dataKey="count" fill="#5ac8fa" />
          </BarChart>
        </ResponsiveContainer>
      </section>
    </div>
  )
}
