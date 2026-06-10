/** Форматирование $ — $0.0001 для микрозатрат, иначе $X.XXXX */
export function fmtCost(v: number): string {
  if (v === 0) return '$0.0000'
  if (v < 0.0001) return `$${v.toExponential(2)}`
  return `$${v.toFixed(4)}`
}

/** Форматирование длительности мс → «1m 23s» / «12.3s» / «456ms» */
export function fmtDuration(ms: number | null): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  const rem = Math.round(s % 60)
  return `${m}m ${rem}s`
}

/** Читабельная дата + время */
export function fmtDatetime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

/** Только дата */
export function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString('ru-RU', {
    day: '2-digit', month: '2-digit',
  })
}

/** Обрезанный SHA */
export function shortSha(sha: string): string {
  return sha.slice(0, 7)
}

/** Числа с тысячными разделителями */
export function fmtNum(n: number): string {
  return n.toLocaleString('ru-RU')
}

/** Процент */
export function fmtPct(v: number): string {
  return `${(v * 100).toFixed(1)}%`
}
