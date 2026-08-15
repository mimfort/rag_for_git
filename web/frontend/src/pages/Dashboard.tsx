import { useState, useEffect } from 'react'
import {
  AreaChart, Area, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { fetchStats, type Stats } from '../api'
import { fmtDate, fmtNum, fmtPct, fmtUnits } from '../utils'

const PERIODS = [
  { label: '7 дн', value: 7 },
  { label: '30 дн', value: 30 },
  { label: '90 дн', value: 90 },
]

const CATEGORY_COLORS: Record<string, string> = {
  correctness:      '#ff4d6d',
  security:         '#ff8c42',
  performance:      '#f5a623',
  style:            '#7c5cff',
  maintainability:  '#2dd4bf',
  other:            '#3d4258',
}

const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ff4d6d',
  high:     '#ff8c42',
  medium:   '#f5c542',
  low:      '#5ac8fa',
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
  animDelay?: string
}

function KpiCard({ label, value, sub, accent = 'var(--indigo)', animDelay = '0s' }: KpiCardProps) {
  return (
    <div
      className="kpi-card"
      style={{ '--kpi-accent': accent, animationDelay: animDelay } as React.CSSProperties}
    >
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

const tooltipStyle = {
  background: 'rgba(14,16,24,0.95)',
  border: '1px solid rgba(255,255,255,0.07)',
  borderRadius: 8,
  fontFamily: 'JetBrains Mono, monospace',
  fontSize: 12,
  boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
  backdropFilter: 'blur(12px)',
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
        <div className="page-subtitle">Агрегированная статистика прогонов агента ревью</div>
      </div>

      <div className="period-selector">
        {PERIODS.map(p => (
          <button
            key={p.value}
            className={`period-btn${days === p.value ? ' active' : ''}`}
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
          <div className="state-icon">⚠</div>
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
              accent="var(--indigo)"
              animDelay="0.05s"
            />
            <KpiCard
              label="Суммарно, усл. ед."
              value={fmtUnits(stats.total_cost)}
              sub="общие затраты"
              accent="var(--amber)"
              animDelay="0.10s"
            />
            <KpiCard
              label="Усл. ед./прогон"
              value={fmtUnits(stats.avg_cost_per_run)}
              sub="среднее"
              accent="var(--amber)"
              animDelay="0.15s"
            />
            <KpiCard
              label="Находок"
              value={fmtNum(stats.total_findings)}
              sub="всего выявлено"
              accent="var(--teal)"
              animDelay="0.20s"
            />
            <KpiCard
              label="Отсев verify"
              value={fmtPct(stats.verify_reject_rate)}
              sub="отклонено верификатором"
              accent="var(--sev-critical)"
              animDelay="0.25s"
            />
          </div>

          <div className="charts-grid">
            {/* Cost over time */}
            <div className="chart-card anim-1">
              <div className="chart-title">Расход (усл. ед.)</div>
              <ResponsiveContainer width="100%" height={190}>
                <AreaChart data={stats.cost_over_time} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="costGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor="#f5a623" stopOpacity={0.25} />
                      <stop offset="95%" stopColor="#f5a623" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="0" vertical={false} />
                  <XAxis dataKey="date" tickFormatter={fmtDate} tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                  <YAxis tickFormatter={(v: number) => fmtUnits(v)} tick={{ fontSize: 10 }} width={54} axisLine={false} tickLine={false} />
                  <Tooltip
                    formatter={(v: number) => [fmtUnits(v), 'Расход, усл. ед.']}
                    labelFormatter={(l: string) => fmtDate(l)}
                    contentStyle={tooltipStyle}
                    labelStyle={{ color: 'rgba(255,255,255,0.4)', marginBottom: 4 }}
                    itemStyle={{ color: '#f5a623' }}
                    cursor={{ stroke: 'rgba(255,255,255,0.05)', strokeWidth: 1 }}
                  />
                  <Area type="monotone" dataKey="cost" stroke="#f5a623" strokeWidth={1.5} fill="url(#costGrad)" dot={false} activeDot={{ r: 3, fill: '#f5a623', stroke: '#08090d', strokeWidth: 2 }} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Runs over time */}
            <div className="chart-card anim-2">
              <div className="chart-title">Прогоны</div>
              <ResponsiveContainer width="100%" height={190}>
                <BarChart data={stats.runs_over_time} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="runsGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%"   stopColor="#7c5cff" stopOpacity={0.9} />
                      <stop offset="100%" stopColor="#7c5cff" stopOpacity={0.3} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="0" vertical={false} />
                  <XAxis dataKey="date" tickFormatter={fmtDate} tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 10 }} width={24} axisLine={false} tickLine={false} />
                  <Tooltip
                    formatter={(v: number) => [v, 'Прогонов']}
                    labelFormatter={(l: string) => fmtDate(l)}
                    contentStyle={tooltipStyle}
                    labelStyle={{ color: 'rgba(255,255,255,0.4)', marginBottom: 4 }}
                    itemStyle={{ color: '#7c5cff' }}
                    cursor={{ fill: 'rgba(124,92,255,0.04)' }}
                  />
                  <Bar dataKey="count" fill="url(#runsGrad)" radius={[4, 4, 0, 0]} maxBarSize={28} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Findings by category */}
            <div className="chart-card anim-3">
              <div className="chart-title">По категориям</div>
              <ResponsiveContainer width="100%" height={210}>
                <BarChart data={stats.findings_by_category} layout="vertical" margin={{ top: 0, right: 8, left: 72, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="0" horizontal={false} />
                  <XAxis type="number" allowDecimals={false} tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                  <YAxis type="category" dataKey="category" tick={{ fontSize: 11, fontFamily: 'JetBrains Mono, monospace' }} axisLine={false} tickLine={false} />
                  <Tooltip
                    formatter={(v: number) => [v, 'Кол-во']}
                    contentStyle={tooltipStyle}
                    labelStyle={{ color: 'rgba(255,255,255,0.4)', marginBottom: 4 }}
                    cursor={{ fill: 'rgba(255,255,255,0.02)' }}
                  />
                  <Bar dataKey="count" radius={[0, 4, 4, 0]} maxBarSize={18}>
                    {stats.findings_by_category.map((entry) => (
                      <Cell key={entry.category} fill={CATEGORY_COLORS[entry.category] ?? '#3d4258'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Findings by severity — donut */}
            <div className="chart-card anim-4">
              <div className="chart-title">По серьёзности</div>
              <ResponsiveContainer width="100%" height={210}>
                <PieChart>
                  <Pie
                    data={stats.findings_by_severity}
                    dataKey="count"
                    nameKey="severity"
                    cx="45%"
                    cy="50%"
                    innerRadius={58}
                    outerRadius={82}
                    paddingAngle={3}
                    strokeWidth={0}
                  >
                    {stats.findings_by_severity.map((entry) => (
                      <Cell key={entry.severity} fill={SEVERITY_COLORS[entry.severity] ?? '#3d4258'} />
                    ))}
                  </Pie>
                  <Tooltip
                    formatter={(v: number, name: string) => [v, SEVERITY_LABELS[name] ?? name]}
                    contentStyle={tooltipStyle}
                    itemStyle={{ color: 'var(--text-primary)' }}
                  />
                  {/* Custom legend on the right */}
                </PieChart>
              </ResponsiveContainer>
              {/* Inline legend */}
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 16px', marginTop: 4, paddingLeft: 4 }}>
                {stats.findings_by_severity.map(entry => (
                  <div key={entry.severity} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{
                      width: 8, height: 8, borderRadius: '50%',
                      background: SEVERITY_COLORS[entry.severity] ?? '#3d4258',
                      flexShrink: 0,
                    }} />
                    <span style={{ fontFamily: 'Satoshi, sans-serif', fontSize: 11, color: 'var(--text-secondary)' }}>
                      {SEVERITY_LABELS[entry.severity] ?? entry.severity}
                    </span>
                    <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: 'var(--text-muted)' }}>
                      {entry.count}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {stats.total_runs === 0 && (
            <div className="state-center" style={{ marginTop: 48 }}>
              <div className="state-icon">○</div>
              <div className="state-msg">Нет данных за выбранный период</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)', fontFamily: 'JetBrains Mono, monospace', marginTop: 4 }}>
                Запустите <span style={{ color: 'var(--amber)' }}>reviewer review owner/repo N</span>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
