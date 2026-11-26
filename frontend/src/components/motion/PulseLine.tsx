/**
 * 实时脉冲线 — 头部「数据心跳」。
 * 用 SVG 描边动画代替 canvas：常驻页面也只有一个合成层，成本可忽略。
 */
export function PulseLine({ live, className }: { live?: boolean; className?: string }) {
  return (
    <svg
      viewBox="0 0 96 22"
      className={className}
      aria-hidden="true"
      preserveAspectRatio="none"
    >
      <path
        d="M0 12h14l3-6 3 12 3-9 3 4h12l3-7 3 14 3-10 3 3h14l3-5 3 9 3-6 3 2h17"
        fill="none"
        stroke={live ? 'hsl(var(--accent))' : 'hsl(var(--fg-muted))'}
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={live ? 'tf-pulse-path' : undefined}
        opacity={live ? 1 : 0.45}
      />
    </svg>
  )
}
