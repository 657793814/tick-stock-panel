/**
 * 字符解码 — MotionKit TextDecode 的移植。
 * 文案逐字从随机字符「解密」到目标文本，市场切换 / 数据重载时给头部一个
 * 明确的「正在接入」信号。中文不参与乱码（避免宽度抖动过大），只做渐显。
 */
import { useEffect, useRef } from 'react'

const GLYPHS = 'ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#$%&*+-/<>[]{}恒指沪深港美纳科量子脉冲数据链路信号'
const SCRAMBLABLE = /[A-Za-z0-9\u4e00-\u9fa5]/

export function DecodeText({
  text,
  className,
  duration = 560,
  trigger,
}: {
  text: string
  className?: string
  duration?: number
  /** 变化时重播（如市场切换、日期变更） */
  trigger?: string | number
}) {
  const ref = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.textContent = text
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    if (reduced || duration <= 0) return

    const chars = [...text]
    const start = performance.now()
    let raf = 0
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      // 解码进度略微前置，尾字符收束更有「落定」感
      const revealed = Math.floor(t * chars.length * 1.4)
      let out = ''
      for (let i = 0; i < chars.length; i++) {
        const ch = chars[i]
        if (SCRAMBLABLE.test(ch) && i >= revealed) {
          out += GLYPHS[(Math.random() * GLYPHS.length) | 0]
        } else {
          out += ch
        }
      }
      el.textContent = out
      if (t < 1) raf = requestAnimationFrame(tick)
      else el.textContent = text
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [text, duration, trigger])

  return <span ref={ref} className={className}>{text}</span>
}
