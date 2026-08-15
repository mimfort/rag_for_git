/**
 * Расход в УСЛОВНЫХ ЕДИНИЦАХ (input-token equivalent), без знака валюты.
 *
 * `total_cost` заполняется взвешенными бакетами токенов
 * (`fresh_in×1 + output×5 + cache_write×1.25 + cache_read×0.1`, PRI-246/PRI-247),
 * а не долларами: колонка не переименована, честность держится подписью в админке.
 */
export function fmtUnits(v: number): string {
  if (!v) return '0'
  if (v < 1) return v.toFixed(4)
  return Math.round(v).toLocaleString('ru-RU')
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
