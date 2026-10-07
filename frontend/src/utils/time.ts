/** 时间显示工具：后端给的是带时区偏移的 ISO-8601，这里按浏览器本地时区渲染。 */

/** 本地时区的日期时间，如 `2026-10-07 22:44`；无法解析时原样返回。 */
export function fmtLocalTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 耗时，如 `42 秒` / `11 分` / `1 小时 3 分`；未知返回空串。 */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return ''
  const s = Math.round(seconds)
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分`
  const h = Math.floor(m / 60)
  return `${h} 小时 ${m % 60} 分`
}

type Status = 'queued' | 'translating' | 'merging' | 'done' | 'failed' | 'cancelled'

/** 终态时间的标签：完成 / 取消 / 失败（进行中的任务不显示）。 */
export function finishLabel(status: Status | string): string {
  if (status === 'done') return '完成时间'
  if (status === 'cancelled') return '取消时间'
  if (status === 'failed') return '失败时间'
  return ''
}
