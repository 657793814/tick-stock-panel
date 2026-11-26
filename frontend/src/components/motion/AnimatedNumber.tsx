/**
 * 数字滚动 — 移植自 UIDesignSystem(MotionKit) CountUp 的核心 rAF 插值。
 *
 * 设计纪律：
 * - 每帧只写 DOM textContent，不进 React state，避免 tick 级别行情刷新时
 *   触发整棵看板 diff（MotionKit 的 ref 直写原则）。
 * - 尊重 prefers-reduced-motion，直接落终值。
 * - resetKey 变化（如市场切换）时从 0 滚入，给「换市场=重新加载」明确的视觉反馈。
 */
import { useEffect, useRef } from 'react'

export const fmtInt = (v: number) => Math.round(v).toLocaleString('zh-CN')
export const fmtOne = (v: number) => v.toFixed(1)
export const fmtTwo = (v: number) => v.toFixed(2)

const easeOutExpo = (t: number) => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t))

export interface AnimatedNumberProps {
  value: number | null | undefined
  format?: (v: number) => string
  duration?: number
  className?: string
  /** 变化时重置动画起点（通常传 market / 数据日期） */
  resetKey?: string | number
}

export function AnimatedNumber({
  value,
  format = fmtInt,
  duration = 700,
  className,
  resetKey,
}: AnimatedNumberProps) {
  const ref = useRef<HTMLSpanElement>(null)
  const prev = useRef<number | null>(null)
  const prevReset = useRef(resetKey)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const target = typeof value === 'number' && Number.isFinite(value) ? value : null
    const finalText = target == null ? '—' : format(target)
    const reset = prevReset.current !== resetKey
    prevReset.current = resetKey
    const from = reset ? 0 : prev.current
    prev.current = target

    const reduced = typeof window !== 'undefined'
      && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (target == null || from == null || from === target || duration <= 0 || reduced) {
      el.textContent = finalText
      return
    }

    let raf = 0
    const start = performance.now()
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      el.textContent = format(from + (target - from) * easeOutExpo(t))
      if (t < 1) raf = requestAnimationFrame(tick)
      else el.textContent = finalText
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [value, format, duration, resetKey])

  const initial = typeof value === 'number' && Number.isFinite(value) ? format(value) : '—'
  return <span ref={ref} className={className}>{initial}</span>
}
