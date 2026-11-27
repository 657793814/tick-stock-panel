import { Database } from 'lucide-react'

import { cn } from '@/lib/cn'

/**
 * 数据来源统一徽章 —— 全站同一套视觉与措辞: 「数据来源 · 同花顺」。
 *
 * 页面凡是展示第三方数据 (扩展页 / 板块成分 / 日频事件因子) 都应挂一枚,
 * 让"这份数据是谁给的"成为页面的一等信息, 而不是藏在设置页里。
 */
export function SourceTag({ source, detail, className }: {
  source: string
  /** 悬停说明: 具体接口 / 落盘表 / 更新节奏 */
  detail?: string
  className?: string
}) {
  return (
    <span
      title={detail}
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-full border border-border bg-elevated/60 px-2 py-0.5',
        'text-[10px] leading-none text-secondary',
        className,
      )}
    >
      <Database className="h-2.5 w-2.5 shrink-0 text-muted" aria-hidden />
      数据来源 · {source}
    </span>
  )
}
