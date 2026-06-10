import { useState, useEffect } from 'react'
import {
  AreaChart, Area, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from 'recharts'
import { fetchStats, type Stats } from '../api'
import { fmtCost, fmtDate, fmtNum, fmtPct } from '../utils'

const PERIODS = [
  { label: '7 дн', value: 7 },
  { label: '30 дн', value: 30 },
  { label: '90 дн', value: 90 },
]

const CATEGORY_COLORS: Record<string, string> = {
  correctness: '#f85149',
  security:    '#ff7b2c',
  performance: '#f0b429',
  style:       '#58a6ff',
  maintainability: '#bc8cff',
  other:       '#6e7681',
}

const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ff4444',
  high:     '#f85149',
  medium:   '#ff7b2c',
  low:      '#58a6ff',
}

const SEVERITY_LABELS: Record<string, string> = {
  critical: 'Критичные',
  high:     'Высокие',
  medium:   'Средние',
  low:      'Низкие',
}

interface KpiCardProps {
  label: string
  value: string
  sub?: string
  accent?: string
}

function KpiCard({ label, value, sub, accent }: KpiCardProps) {
  return (
    <div className="kpi-card" style={{ '--kpi-accent': accent } as React.CSSProperties}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

export default function Dashboard() {
  const [days, setDays] = useState(30)
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetchStats(days)
      .then(setStats)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [days])

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Дашборд</div>
        <div className="page-subtitle">Общая статистика по прогонам агента ревью</div>
      </div>

      <div className="period-selector">
        {PERIODS.map(p => (
          <button
            key={p.value}
            className={`period-btn ${days === p.value ? 'active' : ''}`}
            onClick={() => setDays(p.value)}
          >
            {p.label}
          </button>
        ))}
      </div>

      {loading && (
        <div className="state-center">
          <div className="spinner" />
          <div className="state-msg">Загрузка статистики…</div>
        </div>
      )}

      {error && (
        <div className="state-center">
          <div className="state-icon">!</div>
          <div className="state-msg">Ошибка загрузки</div>
          <div className="state-detail">{error}</div>
        </div>
      )}

      {!loading && !error && stats && (
        <>
          <div className="kpi-grid">
            <KpiCard
              label="Прогонов"
              value={fmtNum(stats.total_runs)}
              sub={`за ${days} дней`}
              accent="var(--blue)"
            />
            <KpiCard
              label="Суммарно $"
              value={fmtCost(stats.total_cost)}
              sub="общие затраты"
              accent="var(--amber)"
            />
            <KpiCard
              label="$/прогон"
              value={fmtCost(stats.avg_cost_per_run)}
              sub="среднее"
              accent="var(--amber)"
            />
            <KpiCard
              label="Находок"
              value={fmtNum(stats.total_findings)}
              sub="всего выявлено"
              accent="var(--green)"
            />
            <KpiCard
              label="% отсева verify"
              value={fmtPct(stats.verify_reject_rate)}
              sub="отклонено верификатором"
              accent="var(--red)"
            />
          </div>

          <div className="charts-grid">
            <div className="chart-card">
              <div className="chart-title">Стоимость во времени ($)</div>
              <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={stats.cost_over_time} margin={{ top: 5, right: 8, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="costGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#f0b429" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#f0b429" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="date" tickFormatter={fmtDate} tick={{ fontSize: 11 }} />
                  <YAxis tickFormatter={(v: number) => `$${v.toFixed(3)}`} tick={{ fontSize: 11 }} width={60} />
                  <Tooltip
                    formatter={(v: number) => [fmtCost(v), 'Стоимость']}
                    labelFormatter={(l: string) => fmtDate(l)}
                    contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 12 }}
                    labelStyle={{ color: 'var(--text-secondary)' }}
                    itemStyle={{ color: 'var(--amber)' }}
                  />
                  <Area type="monotone" dataKey="cost" stroke="#f0b429" strokeWidth={2} fill="url(#costGrad)" dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            <div className="chart-card">
              <div className="chart-title">Прогоны во времени</div>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={stats.runs_over_time} margin={{ top: 5, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="date" tickFormatter={fmtDate} tick={{ fontSize: 11 }} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 11 }} width={30} />
                  <Tooltip
                    formatter={(v: number) => [v, 'Прогонов']}
                    labelFormatter={(l: string) => fmtDate(l)}
                    contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 12 }}
                    labelStyle={{ color: 'var(--text-secondary)' }}
                    itemStyle={{ color: 'var(--blue)' }}
                  />
                  <Bar dataKey="count" fill="#58a6ff" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <div className="chart-card">
              <div className="chart-title">Находки по категориям</div>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={stats.findings_by_category} layout="vertical" margin={{ top: 0, right: 8, left: 60, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                  <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
                  <YAxis type="category" dataKey="category" tick={{ fontSize: 11, fontFamily: 'var(--font-mono)' }} />
                  <Tooltip
                    formatter={(v: number) => [v, 'Кол-во']}
                    contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 12 }}
                    labelStyle={{ color: 'var(--text-secondary)' }}
                  />
                  <Bar dataKey="count" radius={[0, 3, 3, 0]}>
                    {stats.findings_by_category.map((entry) => (
                      <Cell key={entry.category} fill={CATEGORY_COLORS[entry.category] ?? '#6e7681'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            <div className="chart-card">
              <div className="chart-title">Находки по серьёзности</div>
              <ResponsiveContainer width="100%" height={220}>
                <PieChart>
                  <Pie
                    data={stats.findings_by_severity}
                    dataKey="count"
                    nameKey="severity"
                    cx="50%"
                    cy="50%"
                    innerRadius={55}
                    outerRadius={85}
                    paddingAngle={3}
                    label={({ severity, percent }: { severity: string; percent: number }) =>
                      `${SEVERITY_LABELS[severity] ?? severity} ${(percent * 100).toFixed(0)}%`
                    }
                    labelLine={false}
                  >
                    {stats.findings_by_severity.map((entry) => (
                      <Cell key={entry.severity} fill={SEVERITY_COLORS[entry.severity] ?? '#6e7681'} />
                    ))}
                  </Pie>
                  <Tooltip
                    formatter={(v: number, name: string) => [v, SEVERITY_LABELS[name] ?? name]}
                    contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 12 }}
                  />
                  <Legend
                    formatter={(value: string) => SEVERITY_LABELS[value] ?? value}
                    wrapperStyle={{ fontSize: 12, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>

          {stats.total_runs === 0 && (
            <div className="state-center" style={{ marginTop: 40 }}>
              <div className="state-icon">○</div>
              <div className="state-msg">Нет данных за выбранный период</div>
              <div className="text-muted mono" style={{ fontSize: 12 }}>
                Запустите <span className="text-amber">reviewer review owner/repo N</span> чтобы появились записи
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
