/**
 * 环境粒子场 — 背景的「活体」层。
 *
 * 设计纪律（对齐 UIDesignSystem/MotionKit 的零依赖 + ref 直写原则）：
 * - 单个 canvas + 单条 rAF，不建任何 React 节点，帧内不触发 diff。
 * - 粒子受鼠标轻微吸引，并把指针坐标写成 CSS 变量（--tf-cx/--tf-cy），
 *   供 CSS 做「跟随高光」，避免再挂一个 pointermove 监听。
 * - DPR 上限 1.5、粒子上限 72、后台标签页暂停、reduced-motion 只画一帧静态图。
 */
import { useEffect, useRef } from 'react'
import type { Market } from '@/lib/market'

const RGB: Record<Market, [number, number, number]> = {
  cn: [59, 130, 246],
  hk: [34, 211, 238],
  us: [167, 139, 250],
}

interface Dot {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  a: number
}

export function AmbientFX({ market }: { market: Market }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const colorRef = useRef(RGB[market])
  colorRef.current = RGB[market]

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    let w = 0
    let h = 0
    let raf = 0
    let dots: Dot[] = []
    const pointer = { x: -9999, y: -9999, tx: -9999, ty: -9999 }

    const seed = () => {
      const count = Math.min(96, Math.max(34, Math.round((w * h) / 24000)))
      dots = Array.from({ length: count }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.2,
        vy: (Math.random() - 0.5) * 0.2,
        r: 0.9 + Math.random() * 2.1,
        a: 0.26 + Math.random() * 0.5,
      }))
    }

    const draw = (still = false) => {
      const [r, g, b] = colorRef.current
      ctx.clearRect(0, 0, w, h)

      // 粒子间的「数据链」：距离越近越亮，形成神经网络质感
      const linkDist = 132
      ctx.lineWidth = 0.8
      for (let i = 0; i < dots.length; i++) {
        const p = dots[i]
        for (let j = i + 1; j < dots.length; j++) {
          const q = dots[j]
          const dx = p.x - q.x
          const dy = p.y - q.y
          const d2 = dx * dx + dy * dy
          if (d2 > linkDist * linkDist) continue
          const alpha = (1 - Math.sqrt(d2) / linkDist) * 0.26
          ctx.strokeStyle = `rgba(${r},${g},${b},${alpha.toFixed(3)})`
          ctx.beginPath()
          ctx.moveTo(p.x, p.y)
          ctx.lineTo(q.x, q.y)
          ctx.stroke()
        }
      }

      for (const p of dots) {
        // 外晕 + 亮核：两层圆比 shadowBlur 便宜，暗底上才有「发光粒子」质感
        ctx.beginPath()
        ctx.fillStyle = `rgba(${r},${g},${b},${(p.a * 0.16).toFixed(3)})`
        ctx.arc(p.x, p.y, p.r * 3.4, 0, Math.PI * 2)
        ctx.fill()
        ctx.beginPath()
        ctx.fillStyle = `rgba(${r},${g},${b},${p.a})`
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
        ctx.fill()
      }

      // 鼠标光点（仅在指针进入窗口后）
      if (pointer.x > -1000) {
        const grd = ctx.createRadialGradient(pointer.x, pointer.y, 0, pointer.x, pointer.y, 170)
        grd.addColorStop(0, `rgba(${r},${g},${b},0.18)`)
        grd.addColorStop(0.45, `rgba(${r},${g},${b},0.05)`)
        grd.addColorStop(1, `rgba(${r},${g},${b},0)`)
        ctx.fillStyle = grd
        ctx.beginPath()
        ctx.arc(pointer.x, pointer.y, 170, 0, Math.PI * 2)
        ctx.fill()
      }

      if (!still) return
    }

    const step = () => {
      if (!reduced) {
        pointer.x += (pointer.tx - pointer.x) * 0.08
        pointer.y += (pointer.ty - pointer.y) * 0.08
        for (const p of dots) {
          // 指针轻微吸引 + 缓慢漂移
          if (pointer.x > -1000) {
            const dx = pointer.x - p.x
            const dy = pointer.y - p.y
            const d = Math.hypot(dx, dy)
            if (d < 190 && d > 0.001) {
              const force = (1 - d / 190) * 0.012
              p.vx += (dx / d) * force
              p.vy += (dy / d) * force
            }
          }
          p.vx = Math.max(-0.42, Math.min(0.42, p.vx * 0.995))
          p.vy = Math.max(-0.42, Math.min(0.42, p.vy * 0.995))
          p.x += p.vx
          p.y += p.vy
          if (p.x < -8) p.x = w + 8
          if (p.x > w + 8) p.x = -8
          if (p.y < -8) p.y = h + 8
          if (p.y > h + 8) p.y = -8
        }
        draw()
      }
      raf = requestAnimationFrame(step)
    }

    const onResize = () => {
      const dpr = Math.min(1.5, window.devicePixelRatio || 1)
      w = window.innerWidth
      h = window.innerHeight
      canvas.width = Math.floor(w * dpr)
      canvas.height = Math.floor(h * dpr)
      canvas.style.width = `${w}px`
      canvas.style.height = `${h}px`
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      seed()
      draw(reduced)
    }

    const onPointer = (e: PointerEvent) => {
      pointer.tx = e.clientX
      pointer.ty = e.clientY
      const root = document.documentElement
      root.style.setProperty('--tf-cx', `${e.clientX}px`)
      root.style.setProperty('--tf-cy', `${e.clientY}px`)
      root.style.setProperty('--tf-cursor', '1')
    }
    const onLeave = () => {
      pointer.tx = -9999
      pointer.ty = -9999
      document.documentElement.style.setProperty('--tf-cursor', '0')
    }
    const onVisibility = () => {
      if (document.hidden) {
        cancelAnimationFrame(raf)
        raf = 0
      } else if (!raf && !reduced) {
        raf = requestAnimationFrame(step)
      }
    }

    onResize()
    if (!reduced) raf = requestAnimationFrame(step)
    window.addEventListener('resize', onResize)
    window.addEventListener('pointermove', onPointer, { passive: true })
    document.addEventListener('pointerleave', onLeave)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
      window.removeEventListener('pointermove', onPointer)
      document.removeEventListener('pointerleave', onLeave)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  return <canvas ref={canvasRef} aria-hidden className="tf-ambient" />
}
